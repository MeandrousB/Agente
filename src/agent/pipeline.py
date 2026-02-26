from __future__ import annotations

from .collector import MessageCollector
from .db import AgentDB
from .normalizer import normalize_raw_message
from .summarizer import IncrementalSummarizer


class WhatsAppSummaryPipeline:
    def __init__(self, collector: MessageCollector, db: AgentDB, summarizer: IncrementalSummarizer) -> None:
        self.collector = collector
        self.db = db
        self.summarizer = summarizer

    def run_for_group(self, group_name: str) -> str:
        state, last_message_ts = self.db.load_state(group_name)
        raw_messages = self.collector.collect_messages(group_name=group_name, since_timestamp=last_message_ts)

        normalized = [
            msg
            for raw in raw_messages
            if (msg := normalize_raw_message(group_name=group_name, raw=raw)) is not None
        ]

        self.db.save_messages(normalized)
        new_messages = self.db.load_messages_since(group_name, since_ts=last_message_ts)

        result = self.summarizer.summarize(new_messages, state)
        if result.message_count > 0:
            self.db.save_summary(
                group_name,
                result.summary_text,
                result.message_count,
                result.state,
                last_message_ts=new_messages[-1].timestamp,
            )

        return result.summary_text
