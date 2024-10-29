import json

import dash_bootstrap_components as dbc
import pandas as pd
import plotly.express as px
from dash import dcc, html
from sqlalchemy import create_engine
from ui.pages.interfaces.utils import sum_each_index


class TelemetryInterface(object):
    ENGINE = create_engine("sqlite:////tmp/app.db?mode=ro")

    @staticmethod
    def get_logs_df() -> pd.DataFrame:
        df = pd.read_sql(
            """
            SELECT
                severity,
                observed_timestamp,
                body,
                JSON_EXTRACT(attributes, '$.workflow_id') as workflow_id,
                JSON_EXTRACT(attributes, '$.run_id') as run_id,
                JSON_EXTRACT(attributes, '$.activity_type') as activity_type,
                attributes,
                trace_id,
                span_id
            FROM logs
            ORDER BY observed_timestamp DESC
        """,
            con=TelemetryInterface.ENGINE,
        )
        df["run_id"] = (
            "["
            + df["run_id"]
            + "](http://localhost:8233/namespaces/default/workflows/"
            + df["workflow_id"]
            + "/"
            + df["run_id"]
            + "/history)"
        )
        return df

    @staticmethod
    def get_traces_df() -> pd.DataFrame:
        return pd.read_sql_table("traces", con=TelemetryInterface.ENGINE)

    @staticmethod
    def get_metrics_df() -> pd.DataFrame:
        return pd.read_sql_table("metrics", con=TelemetryInterface.ENGINE)

    @staticmethod
    def get_sums_metrics_df() -> pd.DataFrame:
        sums_df = pd.read_sql(
            """
            SELECT
                name,
                description,
                unit,
                datetime(CAST(json_extract(data_points, '$.sum.startTimeUnixNano') AS float) / 1e9, 'unixepoch', 'localtime') as start_time,
                datetime(CAST(json_extract(data_points, '$.sum.timeUnixNano') AS float) / 1e9, 'unixepoch', 'localtime') as end_time,
                CAST(json_extract(data_points, '$.sum.asInt') AS INTEGER) as value
            FROM metrics
            WHERE json_extract(data_points, '$.sum') IS NOT NULL
            ORDER BY start_time DESC
            """,
            con=TelemetryInterface.ENGINE,
        )
        return sums_df

    @classmethod
    def create_sum_metrics_cards(cls):
        sums_metrics_df = cls.get_sums_metrics_df()
        metric_names = sums_metrics_df["name"].unique()
        cards = []
        for sum_metric_name in metric_names:
            df = sums_metrics_df[sums_metrics_df["name"] == sum_metric_name]
            metric_description = df["description"].iloc[0]
            unit = df["unit"].iloc[0]
            card = dbc.Card(
                [
                    dbc.CardHeader(sum_metric_name),
                    dbc.CardBody(
                        [
                            html.P(metric_description),
                            dcc.Graph(
                                id=f"{sum_metric_name}-graph",
                                figure=px.line(
                                    df,
                                    x="start_time",
                                    y="value",
                                    labels={"start_time": "Time", "value": unit},
                                ),
                            ),
                        ]
                    ),
                ]
            )
            cards.append(card)
        return cards

    @staticmethod
    def get_histograms_df() -> pd.DataFrame:
        histogram_metrics_df = pd.read_sql(
            """
        SELECT
            name,
            description,
            unit,
            datetime(CAST(json_extract(data_points, '$.histogram.startTimeUnixNano') AS float) / 1e9, 'unixepoch', 'localtime') as start_time,
            datetime(CAST(json_extract(data_points, '$.histogram.timeUnixNano') AS float) / 1e9, 'unixepoch', 'localtime') as end_time,
            CAST(json_extract(data_points, '$.histogram.count') AS INTEGER) as count,
            CAST(json_extract(data_points, '$.histogram.sum') AS FLOAT) as sum,
            CAST(json_extract(data_points, '$.histogram.min') AS FLOAT) as min,
            CAST(json_extract(data_points, '$.histogram.max') AS FLOAT) as max,
            json_extract(data_points, '$.histogram.bucketCounts') as bucketCounts,
            json_extract(data_points, '$.histogram.explicitBounds') as explicitBounds
        FROM metrics
        WHERE json_extract(data_points, '$.histogram') IS NOT NULL
        ORDER BY start_time DESC
        """,
            con=TelemetryInterface.ENGINE,
        )
        return histogram_metrics_df

    @classmethod
    def create_histogram_cards(cls):
        histogram_df = cls.get_histograms_df()
        metric_names = histogram_df["name"].unique()
        cards = []
        for histogram_metric_name in metric_names:
            df = histogram_df[histogram_df["name"] == histogram_metric_name]
            metric_description = df["description"].iloc[0]
            unit = df["unit"].iloc[0]
            bucket_counts = df["bucketCounts"].apply(lambda x: json.loads(x)).tolist()
            explicit_bounds = (
                df["explicitBounds"].apply(lambda x: json.loads(x)).tolist()
            )

            agg_bucket_counts = sum_each_index(bucket_counts)
            card = dbc.Card(
                [
                    dbc.CardHeader(histogram_metric_name),
                    dbc.CardBody(
                        [
                            html.P(metric_description),
                            dcc.Graph(
                                id=f"{histogram_metric_name}-graph",
                                figure=px.histogram(
                                    x=explicit_bounds[0],
                                    y=agg_bucket_counts[1:],
                                    labels={"x": unit, "y": "Count"},
                                    nbins=len(agg_bucket_counts) - 1,
                                ),
                            ),
                        ]
                    ),
                ]
            )
            cards.append(card)
        return cards
