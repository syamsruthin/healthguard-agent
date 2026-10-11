from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import feedback_rate_limit, get_current_user
from backend.api.schemas import FeedbackIn
from backend.core.errors import NotFoundError
from backend.db.models import Feedback, Message, User
from backend.db.session import get_session
from backend.workers.queue import get_enqueuer

router = APIRouter(prefix="/messages", tags=["feedback"])


@router.post(
    "/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(feedback_rate_limit)],
)
async def give_feedback(
    message_id: uuid.UUID,
    body: FeedbackIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    msg = await session.get(Message, message_id)
    if msg is None or msg.user_id != user.id or msg.role != "assistant":
        raise NotFoundError("Message not found")
    stmt = insert(Feedback).values(
        message_id=message_id, user_id=user.id, rating=body.rating, comment=body.comment
    )
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_feedback_message_user", set_={"rating": body.rating, "comment": body.comment}
        )
    )
    await session.commit()
    if body.rating < 0:  # every thumbs-down gets judged
        await get_enqueuer().enqueue("online_eval", str(message_id), "thumbs_down", job_id=f"oe:{message_id}")
