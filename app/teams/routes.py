"""Teams Bot 라우트"""
from fastapi import APIRouter, Request, Response

from botbuilder.schema import Activity

from app.teams.bot import get_teams_bot
from app.core.router import MessageRouter
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.post("/callback")
async def bot_callback(request: Request) -> Response:
    """Teams Bot 콜백 엔드포인트"""
    try:
        # 요청 본문 파싱
        body = await request.json()
        activity = Activity().from_dict(body)

        # Auth 헤더 추출
        auth_header = request.headers.get("Authorization", "")

        # Bot에 메시지 라우터 핸들러 설정
        bot = get_teams_bot()
        message_router = MessageRouter()
        bot.set_message_handler(message_router.handle_teams_message)

        # Activity 처리
        await bot.process_activity(activity, auth_header)

        return Response(status_code=200)

    except Exception as e:
        logger.error("Bot callback error", error=str(e))
        return Response(status_code=500)


@router.post("/messages")
async def bot_messages(request: Request) -> Response:
    """Teams Bot 메시지 엔드포인트 (별칭)"""
    return await bot_callback(request)
