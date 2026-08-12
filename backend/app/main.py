import logging
import time
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.api import api_router
from app.core.config import settings
from app.core.logging_context import (
    TRACE_ID_HEADER,
    TraceIdFilter,
    reset_session_id,
    reset_trace_id,
    resolve_trace_id,
    set_session_id,
    set_trace_id,
)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


log_dir = Path(settings.LOG_DIR)
log_dir.mkdir(parents=True, exist_ok=True)

detailed_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - [traceId=%(trace_id)s] - "
    "[sessionId=%(session_id)s] - [%(filename)s:%(lineno)d] - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

root_logger = logging.getLogger()
root_logger.setLevel(settings.LOG_LEVEL)
trace_id_filter = TraceIdFilter()

console_handler = logging.StreamHandler()
console_handler.setLevel(settings.LOG_LEVEL)
console_handler.setFormatter(detailed_formatter)
console_handler.addFilter(trace_id_filter)

file_handler = TimedRotatingFileHandler(
    log_dir / "app.log", when="midnight", backupCount=7, encoding="utf-8"
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(detailed_formatter)
file_handler.addFilter(trace_id_filter)
file_handler.suffix = "%Y-%m-%d"

error_handler = TimedRotatingFileHandler(
    log_dir / "error.log", when="midnight", backupCount=7, encoding="utf-8"
)
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(detailed_formatter)
error_handler.addFilter(trace_id_filter)
error_handler.suffix = "%Y-%m-%d"

root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)
root_logger.addHandler(error_handler)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.claude_skill_service import (
        get_claude_skill_service,
        init_claude_skill_service,
    )
    from app.services.session_workspace import get_session_workspace_manager

    logger.info("=" * 60)
    logger.info("启动为单租户模式")
    logger.info("=" * 60)

    skills_dir = Path(settings.SKILLS_DIR)
    workspace_root = Path(settings.WORKSPACE_ROOT)

    try:
        from app.core.mcp_config import load_mcp_config

        mcp_config = load_mcp_config(
            settings.MCP_CONFIG_PATH,
            environment={**settings.claude_agent_env},
        )
        logger.info(
            "✓ MCP 配置已加载: path=%s servers=%s",
            mcp_config.path,
            ",".join(mcp_config.server_names) or "(none)",
        )
    except ValueError as e:
        logger.error("✗ MCP 配置加载失败: %s", e)

    try:
        svc = init_claude_skill_service(skills_dir, workspace_root)
        skill_count = len(svc.scan_skills())
        logger.info(f"✓ ClaudeSkillService 初始化完成: 发现 {skill_count} 个技能")
    except Exception as e:
        logger.error(f"✗ ClaudeSkillService 初始化失败: {e}", exc_info=True)

    try:
        from app.services.open_code_service import init_open_code_service

        init_open_code_service(skills_dir, workspace_root)
        logger.info("✓ OpenCodeService 初始化完成")
    except Exception as e:
        logger.error(f"✗ OpenCodeService 初始化失败: {e}", exc_info=True)

    try:
        ws = get_session_workspace_manager()
        logger.info(f"✓ 会话工作区根目录: {ws.root}")
    except Exception as e:
        logger.error(f"✗ 会话工作区初始化失败: {e}", exc_info=True)

    logger.info(f"Application {settings.PROJECT_NAME} v{settings.VERSION} initialized")
    yield

    try:
        svc = get_claude_skill_service()
        if svc is not None:
            await svc.shutdown()
    except Exception:
        logger.exception("关闭 ClaudeSkillService 失败")


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
        trace_id = resolve_trace_id(request.headers.get(TRACE_ID_HEADER))
        trace_token = set_trace_id(trace_id)
        request.state.trace_id = trace_id
        try:
            logger.info("→ %s %s", request.method, request.url.path)
            response = await call_next(request)
            response.headers[TRACE_ID_HEADER] = trace_id
            session_token = set_session_id(getattr(request.state, "session_id", "-"))
            try:
                logger.info(
                    "← %s %s Status: %s Duration: %.3fs",
                    request.method,
                    request.url.path,
                    response.status_code,
                    time.time() - start_time,
                )
            finally:
                reset_session_id(session_token)
            return response
        except Exception:
            session_token = set_session_id(getattr(request.state, "session_id", "-"))
            try:
                logger.exception(
                    "← %s %s Status: 500 Duration: %.3fs",
                    request.method,
                    request.url.path,
                    time.time() - start_time,
                )
            finally:
                reset_session_id(session_token)
            raise
        finally:
            reset_trace_id(trace_token)

    app.include_router(api_router, prefix=settings.API_V1_STR)

    @app.get("/")
    async def root():
        return {
            "message": f"Welcome to {settings.PROJECT_NAME}",
            "version": settings.VERSION,
            "docs": f"{settings.API_V1_STR}/docs",
        }

    @app.get("/health")
    async def health():
        return {"status": "healthy", "version": settings.VERSION}

    return app


app = create_application()
