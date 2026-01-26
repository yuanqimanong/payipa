from fastapi import APIRouter

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/users/{user_id}")
def read_user(user_id: int):
    return {"user_id": user_id, "name": f"User {user_id}"}
