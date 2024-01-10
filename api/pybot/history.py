import json
from typing import Optional, Self
from uuid import uuid4

from langchain_community.chat_message_histories import RedisChatMessageHistory
from langchain_core.messages import BaseMessage, message_to_dict, messages_from_dict

from pybot.context import session_id
from pybot.utils import utcnow


class PybotMessageHistory(RedisChatMessageHistory):
    """Context aware history which also persists extra information in `additional_kwargs`."""

    opening_remarks: Optional[Self]
    """We add fixed openers to conversations."""

    def __init__(
        self,
        session_id: str,
        url: str = "redis://localhost:6379/0",
        key_prefix: str = "message_store:",
        ttl: int | None = None,
        fixed_messge_history: Self | None = None,
    ):
        super().__init__(session_id, url, key_prefix, ttl)
        self.opening_remarks = fixed_messge_history

    @property
    def key(self) -> str:
        """Construct the record key to use"""
        return self.key_prefix + (session_id.get() or self.session_id)

    def windowed_messages(self, window_size: int = 5) -> list[BaseMessage]:
        """Retrieve the last k pairs of messages from Redis"""
        fixed_messages = []
        if self.opening_remarks:
            fixed_messages = self.opening_remarks.windowed_messages()
        _items = self.redis_client.lrange(self.key, -window_size * 2, -1)
        items = [json.loads(m.decode("utf-8")) for m in _items]
        messages = messages_from_dict(items)
        return fixed_messages + messages

    @property
    def messages(self) -> list[BaseMessage]:  # type: ignore
        """Retrieve the messages from Redis"""
        _items = self.redis_client.lrange(self.key, 0, -1)
        items = [json.loads(m.decode("utf-8")) for m in _items]
        messages = messages_from_dict(items)
        return messages

    def add_message(self, message: BaseMessage) -> None:
        """Append the message to the record in Redis"""
        additional_info = {
            "id": uuid4().hex,
            "sent_at": utcnow().isoformat(),
            "type": "text",
        }
        message.additional_kwargs = additional_info | message.additional_kwargs
        self.redis_client.rpush(self.key, json.dumps(message_to_dict(message)))
        if self.ttl:
            self.redis_client.expire(self.key, self.ttl)
