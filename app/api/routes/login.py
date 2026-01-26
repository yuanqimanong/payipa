from fastapi import APIRouter

router = APIRouter(tags=["login"])


@router.get("/")
def read_root():
    return {"message": "Welcome to Payipa - FastAPI Project!"}
