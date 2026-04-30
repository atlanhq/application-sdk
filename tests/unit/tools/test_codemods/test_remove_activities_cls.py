"""Tests for A6: RemoveActivitiesClsCodemod."""

from __future__ import annotations

from tests.unit.tools.test_codemods.conftest import transform
from tools.migrate_v3.codemods.remove_activities_cls import RemoveActivitiesClsCodemod


class TestActivitiesClsAttribute:
    def test_plain_assignment_removed(self) -> None:
        source = """\
class MyWorkflow:
    activities_cls = MyActivities
    application_name = APP_NAME
"""
        code, changes = transform(source, RemoveActivitiesClsCodemod)
        assert "activities_cls" not in code
        assert "application_name = APP_NAME" in code
        assert any("activities_cls" in c for c in changes)

    def test_annotated_assignment_removed(self) -> None:
        source = """\
from typing import Type

class MyWorkflow:
    activities_cls: Type[MyActivities] = MyActivities
    application_name: str = APP_NAME
"""
        code, changes = transform(source, RemoveActivitiesClsCodemod)
        assert "activities_cls" not in code
        assert "application_name" in code
        assert any("activities_cls" in c for c in changes)

    def test_other_class_attributes_preserved(self) -> None:
        source = """\
class MyWorkflow:
    activities_cls = MyActivities
    default_timeout = 30
    some_flag: bool = True
"""
        code, changes = transform(source, RemoveActivitiesClsCodemod)
        assert "activities_cls" not in code
        assert "default_timeout = 30" in code
        assert "some_flag: bool = True" in code


class TestGetActivitiesMethod:
    def test_static_method_removed(self) -> None:
        source = """\
class MyWorkflow:
    @staticmethod
    def get_activities(activities: MyActivities) -> list:
        return [
            activities.fetch_databases,
            activities.transform_data,
        ]
"""
        code, changes = transform(source, RemoveActivitiesClsCodemod)
        assert "def get_activities" not in code
        assert any("get_activities" in c for c in changes)

    def test_get_asset_extraction_activities_preserved(self) -> None:
        """Only exact name match on 'get_activities' — other helpers are NOT removed."""
        source = """\
class MyWorkflow:
    @staticmethod
    def get_activities(activities: MyActivities) -> list:
        return [activities.extract_data]

    @staticmethod
    def get_asset_extraction_activities() -> list:
        return ["extract_asset_a", "extract_asset_b"]
"""
        code, changes = transform(source, RemoveActivitiesClsCodemod)
        assert "def get_activities" not in code
        assert "def get_asset_extraction_activities" in code

    def test_non_static_get_activities_preserved(self) -> None:
        """A non-static method named get_activities is NOT removed."""
        source = """\
class MyWorkflow:
    def get_activities(self, activities):
        return [activities.fetch_databases]
"""
        code, changes = transform(source, RemoveActivitiesClsCodemod)
        assert "def get_activities" in code
        assert not changes


class TestActivitiesInstanceLine:
    def test_instance_assignment_removed(self) -> None:
        source = """\
class MyWorkflow:
    async def run(self, config):
        activities_instance = self.activities_cls()
        result = await workflow.execute_activity_method(
            activities_instance.fetch_databases, args=[config]
        )
"""
        code, changes = transform(source, RemoveActivitiesClsCodemod)
        assert "activities_instance = self.activities_cls()" not in code
        assert any("activities_instance" in c for c in changes)

    def test_annotated_instance_assignment_removed(self) -> None:
        source = """\
class MyWorkflow:
    async def run(self, config):
        activities_instance: MyActivities = self.activities_cls()
        await some_call(activities_instance.method)
"""
        code, changes = transform(source, RemoveActivitiesClsCodemod)
        assert "activities_instance: MyActivities = self.activities_cls()" not in code

    def test_unrelated_activities_instance_preserved(self) -> None:
        """Only remove when RHS is exactly self.activities_cls()."""
        source = """\
class MyWorkflow:
    async def run(self, config):
        activities_instance = MyActivities()
"""
        code, changes = transform(source, RemoveActivitiesClsCodemod)
        # RHS is MyActivities(), not self.activities_cls() — keep it
        assert "activities_instance = MyActivities()" in code
        assert not changes


class TestAllThreeTogether:
    def test_all_three_removed_in_one_class(self) -> None:
        source = """\
class MyWorkflow:
    activities_cls: Type[MyActivities] = MyActivities
    application_name: str = APP_NAME

    @staticmethod
    def get_activities(activities: MyActivities) -> list:
        return [activities.fetch_databases]

    async def run(self, config):
        activities_instance = self.activities_cls()
        await do_something(activities_instance)
"""
        code, changes = transform(source, RemoveActivitiesClsCodemod)
        assert "activities_cls" not in code
        assert "def get_activities" not in code
        assert "activities_instance = self.activities_cls()" not in code
        assert "application_name" in code
        assert len(changes) == 3
