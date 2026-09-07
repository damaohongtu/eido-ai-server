from fastapi import APIRouter, HTTPException

from app.core.config import settings

router = APIRouter()


@router.get("/models")
async def list_models():
    """列出所有已配置的模型（不暴露密钥）。"""
    models = settings.models
    default_id = settings.default_model_id
    return {
        "models": [
            {
                "id": m.id,
                "name": m.display_name(),
                "harness": m.harness,
                "is_default": m.id == default_id,
            }
            for m in models
        ],
        "default_model_id": default_id,
    }


@router.get("/models/{model_id}")
async def get_model(model_id: str):
    """获取单个模型信息（不暴露密钥）。"""
    model = settings.get_model(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"模型不存在: {model_id}")
    return {
        "id": model.id,
        "name": model.display_name(),
        "harness": model.harness,
        "model": model.model,
        "is_default": model.id == settings.default_model_id,
    }
