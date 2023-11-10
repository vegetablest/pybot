from typing import Annotated

from fastapi import APIRouter, Depends, Header, WebSocket, WebSocketDisconnect
from langchain.chains import ConversationChain
from langchain.llms import BaseLLM, HuggingFaceTextGenInference
from langchain.memory import RedisChatMessageHistory
from loguru import logger

from pybot.callbacks import (
    StreamingLLMCallbackHandler,
    UpdateConversationCallbackHandler,
)
from pybot.config import settings
from pybot.history import CustomRedisChatMessageHistory
from pybot.memory import FlexConversationBufferWindowMemory
from pybot.models import Conversation as ORMConversation
from pybot.prompts.vicuna import (
    ai_prefix,
    ai_suffix,
    human_prefix,
    human_suffix,
    prompt,
)
from pybot.schemas import (
    ChatMessage,
    Conversation,
    ConversationDetail,
    UpdateConversation,
)
from pybot.utils import UserIdHeader

router = APIRouter(
    prefix="/api",
    tags=["conversation"],
)


def get_message_history() -> RedisChatMessageHistory:
    return CustomRedisChatMessageHistory(
        url=str(settings.redis_om_url),
        session_id="sid",  # a fake session id as it is required
    )


def get_llm() -> BaseLLM:
    return HuggingFaceTextGenInference(
        inference_server_url=str(settings.inference_server_url),
        max_new_tokens=1024,
        temperature=0.8,
        top_p=0.9,
        repetition_penalty=1.01,
        stop_sequences=["</s>"],
        streaming=True,
    )


@router.get("/conversations", response_model=list[Conversation])
async def get_conversations(userid: Annotated[str | None, UserIdHeader()] = None):
    convs = await ORMConversation.find(ORMConversation.owner == userid).all()
    convs.sort(key=lambda x: x.updated_at, reverse=True)
    return [Conversation(**conv.dict()) for conv in convs]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str,
    history: Annotated[RedisChatMessageHistory, Depends(get_message_history)],
    userid: Annotated[str | None, UserIdHeader()] = None,
):
    conv = await ORMConversation.get(conversation_id)
    history.session_id = f"{userid}:{conversation_id}"
    return ConversationDetail(
        messages=[
            ChatMessage.from_lc(lc_message=message, conv_id=conversation_id, from_="ai")
            if message.type == "ai"
            else ChatMessage.from_lc(
                lc_message=message, conv_id=conversation_id, from_=userid
            )
            for message in history.messages
        ],
        **conv.dict(),
    )


@router.post("/conversations", status_code=201, response_model=ConversationDetail)
async def create_conversation(userid: Annotated[str | None, UserIdHeader()] = None):
    conv = ORMConversation(title=f"New chat", owner=userid)
    await conv.save()
    return ConversationDetail(**conv.dict())


@router.put("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: str,
    payload: UpdateConversation,
    userid: Annotated[str | None, UserIdHeader()] = None,
):
    conv = await ORMConversation.get(conversation_id)
    conv.title = payload.title
    await conv.save()


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str,
    userid: Annotated[str | None, UserIdHeader()] = None,
):
    await ORMConversation.delete(conversation_id)


@router.websocket("/chat")
async def generate(
    websocket: WebSocket,
    llm: Annotated[BaseLLM, Depends(get_llm)],
    history: Annotated[RedisChatMessageHistory, Depends(get_message_history)],
    userid: Annotated[str | None, UserIdHeader()] = None,
):
    await websocket.accept()
    memory = FlexConversationBufferWindowMemory(
        human_prefix=human_prefix,
        ai_prefix=ai_prefix,
        human_suffix=human_suffix,
        ai_suffix=ai_suffix,
        memory_key="history",
        chat_memory=history,
    )
    conversation_chain: ConversationChain = ConversationChain(
        llm=llm,
        prompt=prompt,
        verbose=False,
        memory=memory,
    )

    while True:
        try:
            payload: str = await websocket.receive_text()
            message = ChatMessage.model_validate_json(payload)
            history.session_id = f"{userid}:{message.conversation}"
            streaming_callback = StreamingLLMCallbackHandler(
                websocket, message.conversation
            )
            update_conversation_callback = UpdateConversationCallbackHandler(
                message.conversation
            )
            await conversation_chain.arun(
                message.content,
                callbacks=[streaming_callback, update_conversation_callback],
            )
        except WebSocketDisconnect:
            logger.info("websocket disconnected")
            return
        except Exception as e:
            logger.error(f"Something goes wrong, err: {e}")
