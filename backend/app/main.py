from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api.v1.api import api_router
import logging
from logging.handlers import TimedRotatingFileHandler
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


log_dir = Path(settings.LOG_DIR)
log_dir.mkdir(parents=True, exist_ok=True)

detailed_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

root_logger = logging.getLogger()
root_logger.setLevel(settings.LOG_LEVEL)

console_handler = logging.StreamHandler()
console_handler.setLevel(settings.LOG_LEVEL)
console_handler.setFormatter(detailed_formatter)

file_handler = TimedRotatingFileHandler(
    log_dir / 'app.log', when='midnight', backupCount=7, encoding='utf-8'
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(detailed_formatter)
file_handler.suffix = '%Y-%m-%d'

error_handler = TimedRotatingFileHandler(
    log_dir / 'error.log', when='midnight', backupCount=7, encoding='utf-8'
)
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(detailed_formatter)
error_handler.suffix = '%Y-%m-%d'

root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)
root_logger.addHandler(error_handler)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.claude_skill_service import init_claude_skill_service
    from app.services.session_workspace import get_session_workspace_manager

    logger.info("=" * 60)
    logger.info("启动为单租户模式")
    logger.info("=" * 60)

    skills_dir = Path(settings.SKILLS_DIR)
    workspace_root = Path(settings.WORKSPACE_ROOT)

    try:
        svc = init_claude_skill_service(skills_dir, workspace_root)
        skill_count = len(svc.scan_skills())
        logger.info(f"✓ ClaudeSkillService 初始化完成: 发现 {skill_count} 个技能")
    except Exception as e:
        logger.error(f"✗ ClaudeSkillService 初始化失败: {e}", exc_info=True)

    try:
        from app.services.open_harness_service import init_open_harness_service
        init_open_harness_service(skills_dir, workspace_root)
        logger.info("✓ OpenHarnessService 初始化完成")
    except Exception as e:
        logger.error(f"✗ OpenHarnessService 初始化失败: {e}", exc_info=True)

    try:
        ws = get_session_workspace_manager()
        logger.info(f"✓ 会话工作区根目录: {ws.root}")
    except Exception as e:
        logger.error(f"✗ 会话工作区初始化失败: {e}", exc_info=True)

    logger.info(f"Application {settings.PROJECT_NAME} v{settings.VERSION} initialized")
    yield


def create_application() -> FastAPI:

    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        openapi_url=f"{settings.API_V1_STR}/openapi.json",
        docs_url=f"{settings.API_V1_STR}/docs",
        redoc_url=f"{settings.API_V1_STR}/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.BACKEND_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start_time = time.time()
        logger.info(f"→ {request.method} {request.url.path}")
        response = await call_next(request)
        process_time = time.time() - start_time
        logger.info(
            f"← {request.method} {request.url.path} "
            f"Status: {response.status_code} "
            f"Duration: {process_time:.3f}s"
        )
        return response

    app.include_router(api_router, prefix=settings.API_V1_STR)

    @app.get("/")
    async def root():
        return {
            "message": f"Welcome to {settings.PROJECT_NAME}",
            "version": settings.VERSION,
            "docs": f"{settings.API_V1_STR}/docs"
        }

    @app.get("/health")
    async def health():
        return {"status": "healthy", "version": settings.VERSION}

    return app


app = create_application()
