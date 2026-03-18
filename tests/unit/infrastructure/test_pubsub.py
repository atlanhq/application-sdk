"""Unit tests for InMemoryPubSub."""

from application_sdk.infrastructure.pubsub import InMemoryPubSub, Message, PubSubError


class TestInMemoryPubSub:
    """Tests for InMemoryPubSub."""

    async def test_publish_delivers_to_subscriber(self) -> None:
        """Test that a published message is delivered to a subscriber."""
        pubsub = InMemoryPubSub()
        received: list[Message] = []

        async def handler(msg: Message) -> None:
            received.append(msg)

        await pubsub.subscribe("my-topic", handler)
        await pubsub.publish("my-topic", {"key": "value"})

        assert len(received) == 1
        assert received[0].data == {"key": "value"}
        assert received[0].topic == "my-topic"

    async def test_publish_with_metadata(self) -> None:
        """Test that metadata is included in the delivered message."""
        pubsub = InMemoryPubSub()
        received: list[Message] = []

        async def handler(msg: Message) -> None:
            received.append(msg)

        await pubsub.subscribe("topic", handler)
        await pubsub.publish("topic", {"x": 1}, metadata={"trace": "abc"})

        assert received[0].metadata == {"trace": "abc"}

    async def test_publish_with_no_metadata_defaults_to_empty_dict(self) -> None:
        """Test that metadata defaults to empty dict when not provided."""
        pubsub = InMemoryPubSub()
        received: list[Message] = []

        async def handler(msg: Message) -> None:
            received.append(msg)

        await pubsub.subscribe("topic", handler)
        await pubsub.publish("topic", {"x": 1})

        assert received[0].metadata == {}

    async def test_publish_delivers_to_multiple_subscribers(self) -> None:
        """Test that publishing delivers to all active subscribers."""
        pubsub = InMemoryPubSub()
        received_a: list[Message] = []
        received_b: list[Message] = []

        async def handler_a(msg: Message) -> None:
            received_a.append(msg)

        async def handler_b(msg: Message) -> None:
            received_b.append(msg)

        await pubsub.subscribe("topic", handler_a)
        await pubsub.subscribe("topic", handler_b)
        await pubsub.publish("topic", {"n": 1})

        assert len(received_a) == 1
        assert len(received_b) == 1

    async def test_publish_does_not_deliver_to_other_topics(self) -> None:
        """Test that messages are only delivered to subscribers of the correct topic."""
        pubsub = InMemoryPubSub()
        received: list[Message] = []

        async def handler(msg: Message) -> None:
            received.append(msg)

        await pubsub.subscribe("topic-a", handler)
        await pubsub.publish("topic-b", {"x": 1})

        assert received == []

    async def test_subscribe_returns_active_subscription(self) -> None:
        """Test that subscribe returns a subscription with is_active=True."""
        pubsub = InMemoryPubSub()

        async def handler(msg: Message) -> None:
            pass

        sub = await pubsub.subscribe("topic", handler)

        assert sub.is_active is True
        assert sub.topic == "topic"

    async def test_unsubscribe_deactivates_subscription(self) -> None:
        """Test that unsubscribe deactivates the subscription."""
        pubsub = InMemoryPubSub()

        async def handler(msg: Message) -> None:
            pass

        sub = await pubsub.subscribe("topic", handler)
        await sub.unsubscribe()

        assert sub.is_active is False

    async def test_unsubscribed_handler_does_not_receive_messages(self) -> None:
        """Test that an unsubscribed handler does not receive further messages."""
        pubsub = InMemoryPubSub()
        received: list[Message] = []

        async def handler(msg: Message) -> None:
            received.append(msg)

        sub = await pubsub.subscribe("topic", handler)
        await pubsub.publish("topic", {"first": True})
        await sub.unsubscribe()
        await pubsub.publish("topic", {"second": True})

        assert len(received) == 1
        assert received[0].data == {"first": True}

    async def test_get_published_returns_all_messages(self) -> None:
        """Test that get_published returns all published messages."""
        pubsub = InMemoryPubSub()
        await pubsub.publish("t1", {"a": 1})
        await pubsub.publish("t2", {"b": 2})
        await pubsub.publish("t1", {"c": 3})

        all_msgs = pubsub.get_published()

        assert len(all_msgs) == 3

    async def test_get_published_filtered_by_topic(self) -> None:
        """Test that get_published filters messages by topic."""
        pubsub = InMemoryPubSub()
        await pubsub.publish("t1", {"a": 1})
        await pubsub.publish("t2", {"b": 2})
        await pubsub.publish("t1", {"c": 3})

        t1_msgs = pubsub.get_published(topic="t1")

        assert len(t1_msgs) == 2
        assert all(m.topic == "t1" for m in t1_msgs)

    async def test_get_published_no_topic_filter_returns_copy(self) -> None:
        """Test that get_published returns a copy of the list (not the internal list)."""
        pubsub = InMemoryPubSub()
        await pubsub.publish("t", {"x": 1})

        msgs = pubsub.get_published()
        msgs.clear()

        # Internal list is unchanged
        assert len(pubsub.get_published()) == 1

    async def test_clear_removes_all_state(self) -> None:
        """Test that clear() removes all subscriptions and published messages."""
        pubsub = InMemoryPubSub()
        received: list[Message] = []

        async def handler(msg: Message) -> None:
            received.append(msg)

        await pubsub.subscribe("topic", handler)
        await pubsub.publish("topic", {"x": 1})
        pubsub.clear()

        # After clear, published history is cleared
        assert pubsub.get_published() == []

        # After clear, subscriptions are gone — second message goes unhandled
        await pubsub.publish("topic", {"y": 2})
        # Only the message after clear is in published (clear reset the list)
        assert len(pubsub.get_published()) == 1
        assert pubsub.get_published()[0].data == {"y": 2}
        # Handler was only called for the first message
        assert len(received) == 1

    async def test_published_messages_have_unique_ids(self) -> None:
        """Test that each published message has a unique ID."""
        pubsub = InMemoryPubSub()
        await pubsub.publish("t", {"a": 1})
        await pubsub.publish("t", {"b": 2})

        msgs = pubsub.get_published()
        ids = {m.id for m in msgs}

        assert len(ids) == 2

    async def test_multiple_publishes_accumulate(self) -> None:
        """Test that multiple publishes accumulate in get_published."""
        pubsub = InMemoryPubSub()
        for i in range(5):
            await pubsub.publish("topic", {"i": i})

        assert len(pubsub.get_published()) == 5


class TestPubSubError:
    """Tests for PubSubError."""

    def test_error_code_is_included_in_str(self) -> None:
        """Test that the error code is included in string representation."""
        err = PubSubError("something failed")
        assert "AAF-INF-002" in str(err)

    def test_topic_and_operation_included_in_str(self) -> None:
        """Test that topic and operation are included in string representation."""
        err = PubSubError("failed", topic="my-topic", operation="publish")
        s = str(err)
        assert "topic=my-topic" in s
        assert "operation=publish" in s

    def test_cause_included_in_str(self) -> None:
        """Test that cause is included in string representation."""
        cause = ConnectionError("broker unavailable")
        err = PubSubError("failed", cause=cause)
        assert "ConnectionError" in str(err)
