from abc import ABC, abstractmethod
from dependency_injector import containers, providers
from typing import Dict, Any, List

# Resource classes
class Resource:
    def __init__(self):
        pass

class SQLResource(Resource):
    def __init__(self, credentials=None):
        self.credentials = credentials if credentials else []
        self.engine = self.create_engine()  # Mocked for simplicity
        self.connection = None
        self.with_credentials(credentials)

    def create_engine(self):
        # Placeholder for engine creation logic
        return "SQL Engine Instance"

    def with_credentials(self, credentials):
        self.credentials = credentials
        return self

    def run_query(self, query: str, batch_size: int = 100000):
        print('in run query')
        pass

class RESTResource(Resource):
    def __init__(self):
        pass 

class PostgresResource(SQLResource):
    pass




# Controllers/Interfaces
class Controller(ABC):
    pass

class WorkflowAuthController(Controller, ABC):
    """
    Base class for workflow auth controllers
    """
    sql_resource: SQLResource

    def __init__(self, sql_resource: SQLResource = None):
        if sql_resource:
            self.with_sql_resource(sql_resource)

    def with_sql_resource(self, sql_resource):
        self.sql_resource = sql_resource
        return self

    @abstractmethod
    def test_auth(self, credential: Dict[str, Any]) -> bool:
        raise NotImplementedError

class SampleWorkflowAuthController(WorkflowAuthController):
    def __init__(self, sql_resource: SQLResource = None):
        super().__init__(sql_resource)

    def test_auth(self, credential: Dict[str, Any]) -> bool:
        # Sample logic for auth testing
        self.sql_resource.run_query('a')

class WorkflowMetadataController(Controller, ABC):
    def __init__(self):
        pass

    @abstractmethod
    def fetch_metadata(self, credential: Dict[str, Any]) -> List[Dict[str, str]]:
        raise NotImplementedError

class SampleWorkflowMetadataController(WorkflowMetadataController):
    def __init__(self):
        pass

    def fetch_metadata(self, credential: Dict[str, Any]) -> List[Dict[str, str]]:
        # Sample logic for fetching metadata
        raise NotImplementedError

class WorkflowPreflightCheckController(Controller, ABC):
    @abstractmethod
    def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

class SampleWorkflowPreflightCheckController(WorkflowPreflightCheckController):
    def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # Sample logic for preflight check
        raise NotImplementedError

class WorkflowWorkerController(Controller, ABC):
    pass

class SampleWorkflowWorkerController(WorkflowWorkerController, ABC):
    pass




# Builder
class WorkflowBuilder(ABC):
    def __init__(self):
        pass

    def with_worker(self, worker_controller: WorkflowWorkerController):
        self.worker_controller = worker_controller
        return self

    def build(self):
        # Ensure all dependencies are set before building
        print("Workflow Builder is initialized with all components.")
        return self

# SQL Workflow Builder
class SQLWorkflowBuilder(WorkflowBuilder):
    def __init__(self, sql_resource: SQLResource = None, 
                 auth_controller: WorkflowAuthController = None, 
                 preflight_check_controller: WorkflowPreflightCheckController = None, 
                 metadata_controller: WorkflowMetadataController = None, 
                 worker_controller: WorkflowWorkerController = None):
        super().__init__()

        # Resources
        self.sql_resource = sql_resource
        self.with_sql_resource(sql_resource)

        # Controllers
        self.auth_controller = auth_controller
        self.with_auth(auth_controller)

        self.preflight_check_controller = None
        if preflight_check_controller:
            self.with_preflight(preflight_check_controller)

        self.metadata_controller = None
        if metadata_controller:
            self.with_metadata(metadata_controller)

        self.worker_controller = None
        if worker_controller:
            self.with_worker(worker_controller)

    def with_sql_resource(self, sql_resource: SQLResource):
        self.sql_resource = sql_resource
        return self

    def with_auth(self, auth_controller: WorkflowAuthController):
        self.auth_controller = auth_controller.with_sql_resource(self.sql_resource)
        return self

    def with_preflight(self, preflight_check_controller: WorkflowPreflightCheckController):
        self.preflight_check_controller = preflight_check_controller
        return self

    def with_metadata(self, metadata_controller: WorkflowMetadataController):
        self.metadata_controller = metadata_controller
        return self

    def with_worker(self, worker_controller: WorkflowWorkerController):
        super().with_worker(worker_controller)
        return self

    def build(self):
        assert self.sql_resource is not None, "SQL resource must be defined"
        super().build()




# Example usage
if __name__ == "__main__":
    # Instantiate builder and inject dependencies
    sql_workflow_builder = SQLWorkflowBuilder(
        sql_resource=SQLResource(),
        auth_controller=SampleWorkflowAuthController(),
        preflight_check_controller=SampleWorkflowPreflightCheckController(),
        worker_controller=SampleWorkflowWorkerController(),
        metadata_controller=SampleWorkflowMetadataController()
    )

    sql_workflow_builder.build()
    sql_workflow_builder.auth_controller.test_auth({})
