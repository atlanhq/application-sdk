class DiffCalculator:
    def __init__(self):
        self.logger = get_logger(__name__)

    def calculate_diff(
        self, df: daft.DataFrame, publish_state_df: daft.DataFrame
    ) -> daft.DataFrame:
        pass
