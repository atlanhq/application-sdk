"""Unit tests for the in-memory semaphore implementation."""

import unittest

from application_sdk.common.semaphore.memory import InMemorySemaphore


class TestInMemorySemaphore(unittest.TestCase):
    """Test cases for the InMemorySemaphore class."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        # Clear any existing semaphores
        InMemorySemaphore._semaphores.clear()

    def test_acquire_single_permit_success(self) -> None:
        """Test successful acquisition of a single permit."""
        result = InMemorySemaphore.acquire(
            resource_id="test_resource",
            lock_owner="test_owner",
        )
        self.assertTrue(result)
        self.assertEqual(InMemorySemaphore.get_permits("test_resource"), 0)

    def test_acquire_multiple_permits_success(self) -> None:
        """Test successful acquisition of multiple permits."""
        result = InMemorySemaphore.acquire(
            resource_id="test_resource",
            lock_owner="test_owner",
            permits=3,
            max_permits=5,
        )
        self.assertTrue(result)
        self.assertEqual(InMemorySemaphore.get_permits("test_resource"), 2)

    def test_acquire_invalid_permits(self) -> None:
        """Test acquisition with invalid number of permits."""
        result = InMemorySemaphore.acquire(
            resource_id="test_resource",
            lock_owner="test_owner",
            permits=0,
        )
        self.assertFalse(result)
        self.assertEqual(InMemorySemaphore.get_permits("test_resource"), 0)

    def test_acquire_too_many_permits(self) -> None:
        """Test acquisition of more permits than available."""
        result = InMemorySemaphore.acquire(
            resource_id="test_resource",
            lock_owner="test_owner",
            permits=2,
            max_permits=1,
        )
        self.assertFalse(result)
        self.assertEqual(InMemorySemaphore.get_permits("test_resource"), 1)

    def test_acquire_remaining_permits(self) -> None:
        """Test acquisition of remaining permits by same owner."""
        # First acquire some permits
        result1 = InMemorySemaphore.acquire(
            resource_id="test_resource",
            lock_owner="test_owner",
            permits=2,
            max_permits=5,
        )
        self.assertTrue(result1)
        self.assertEqual(InMemorySemaphore.get_permits("test_resource"), 3)

        # Acquire remaining permits
        result2 = InMemorySemaphore.acquire(
            resource_id="test_resource",
            lock_owner="test_owner",
            permits=3,
        )
        self.assertTrue(result2)
        self.assertEqual(InMemorySemaphore.get_permits("test_resource"), 0)

    def test_acquire_permits_multiple_owners(self) -> None:
        """Test acquisition of permits by multiple owners."""
        # First owner acquires permits
        result1 = InMemorySemaphore.acquire(
            resource_id="test_resource",
            lock_owner="owner1",
            permits=2,
            max_permits=5,
        )
        self.assertTrue(result1)
        self.assertEqual(InMemorySemaphore.get_permits("test_resource"), 3)

        # Second owner acquires permits
        result2 = InMemorySemaphore.acquire(
            resource_id="test_resource",
            lock_owner="owner2",
            permits=2,
        )
        self.assertTrue(result2)
        self.assertEqual(InMemorySemaphore.get_permits("test_resource"), 1)

    def test_release_permits_success(self) -> None:
        """Test successful release of permits."""
        # First acquire permits
        acquire_result = InMemorySemaphore.acquire(
            resource_id="test_resource",
            lock_owner="test_owner",
            permits=3,
            max_permits=5,
        )
        self.assertTrue(acquire_result)
        self.assertEqual(InMemorySemaphore.get_permits("test_resource"), 2)

        # Release some permits
        release_result = InMemorySemaphore.release(
            resource_id="test_resource",
            lock_owner="test_owner",
            permits=2,
        )
        self.assertTrue(release_result)
        self.assertEqual(InMemorySemaphore.get_permits("test_resource"), 4)

    def test_release_invalid_permits(self) -> None:
        """Test release with invalid number of permits."""
        result = InMemorySemaphore.release(
            resource_id="test_resource",
            lock_owner="test_owner",
            permits=0,
        )
        self.assertFalse(result)

    def test_release_nonexistent_resource(self) -> None:
        """Test release for nonexistent resource."""
        result = InMemorySemaphore.release(
            resource_id="nonexistent",
            lock_owner="test_owner",
        )
        self.assertFalse(result)

    def test_release_wrong_owner(self) -> None:
        """Test release by wrong owner."""
        # First acquire permits
        acquire_result = InMemorySemaphore.acquire(
            resource_id="test_resource",
            lock_owner="owner1",
            permits=2,
            max_permits=5,
        )
        self.assertTrue(acquire_result)

        # Try to release with wrong owner
        release_result = InMemorySemaphore.release(
            resource_id="test_resource",
            lock_owner="owner2",
            permits=1,
        )
        self.assertFalse(release_result)
        self.assertEqual(InMemorySemaphore.get_permits("test_resource"), 3)

    def test_release_too_many_permits(self) -> None:
        """Test release of more permits than held."""
        # First acquire permits
        acquire_result = InMemorySemaphore.acquire(
            resource_id="test_resource",
            lock_owner="test_owner",
            permits=2,
            max_permits=5,
        )
        self.assertTrue(acquire_result)

        # Try to release more permits than held
        release_result = InMemorySemaphore.release(
            resource_id="test_resource",
            lock_owner="test_owner",
            permits=3,
        )
        self.assertFalse(release_result)
        self.assertEqual(InMemorySemaphore.get_permits("test_resource"), 3)

    def test_get_permits_nonexistent_resource(self) -> None:
        """Test getting permits for nonexistent resource."""
        permits = InMemorySemaphore.get_permits("nonexistent")
        self.assertEqual(permits, 0)

    def test_multiple_resources(self) -> None:
        """Test handling of multiple resources."""
        # Acquire permits for first resource
        result1 = InMemorySemaphore.acquire(
            resource_id="resource1",
            lock_owner="owner1",
            permits=2,
            max_permits=3,
        )
        self.assertTrue(result1)
        self.assertEqual(InMemorySemaphore.get_permits("resource1"), 1)

        # Acquire permits for second resource
        result2 = InMemorySemaphore.acquire(
            resource_id="resource2",
            lock_owner="owner2",
            permits=3,
            max_permits=5,
        )
        self.assertTrue(result2)
        self.assertEqual(InMemorySemaphore.get_permits("resource2"), 2)

        # Release permits from first resource
        release_result = InMemorySemaphore.release(
            resource_id="resource1",
            lock_owner="owner1",
            permits=1,
        )
        self.assertTrue(release_result)
        self.assertEqual(InMemorySemaphore.get_permits("resource1"), 2)
        self.assertEqual(InMemorySemaphore.get_permits("resource2"), 2)