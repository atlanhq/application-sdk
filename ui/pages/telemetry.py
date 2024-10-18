import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, callback, html

from ui.pages.interfaces.telemetry import TelemetryInterface
from ui.pages.interfaces.utils import create_ag_grid

dash.register_page(__name__, name="📡 Telemetry")

telemetry_interface = TelemetryInterface()

layout = html.Div(
    [
        dbc.Container(
            [
                html.H1("📡 Telemetry Dashboard"),
                dbc.Card(
                    [
                        dbc.CardHeader(
                            dbc.Tabs(
                                [
                                    dbc.Tab(label="Logs", tab_id="logs-tab"),
                                    dbc.Tab(label="Traces", tab_id="traces-tab"),
                                    dbc.Tab(label="Metrics", tab_id="metrics-tab"),
                                ],
                                id="card-tabs",
                                active_tab="logs-tab",
                            )
                        ),
                        dbc.CardBody(html.P(id="card-content", className="card-text")),
                    ]
                ),
            ],
            fluid=True,
        )
    ]
)


def sum_each_index(list_of_lists):
    """Sums the elements at each index across all sublists."""

    result = []
    if not list_of_lists:
        return result

    max_length = max(len(sublist) for sublist in list_of_lists)

    for i in range(max_length):
        index_sum = 0
        for sublist in list_of_lists:
            if i < len(sublist):
                index_sum += int(sublist[i])
        result.append(index_sum)

    return result


@callback(Output("card-content", "children"), [Input("card-tabs", "active_tab")])
def render_content(active_tab):
    if active_tab == "logs-tab":
        logs_df = telemetry_interface.get_logs_df()
        column_defs = []
        for column in logs_df.columns:
            if column == "run_id":
                column_defs.append(
                    {
                        "field": column,
                        "headerName": column,
                        "linkTarget": "_blank",
                        "cellRenderer": "markdown",
                    }
                )
            else:
                column_defs.append({"field": column})
        return create_ag_grid("logs-table", logs_df, column_defs)
    elif active_tab == "traces-tab":
        return create_ag_grid("traces-table", telemetry_interface.get_traces_df())
    elif active_tab == "metrics-tab":
        # sums_df = metrics_df.filter()
        # Get all the metric names in the df
        sums_cards = telemetry_interface.create_sum_metrics_cards()
        rows = []
        if sums_cards:
            rows.append(html.H4("Sum Metrics"))
        for x, y in zip(sums_cards[::2], sums_cards[1::2]):
            rows.append(
                dbc.Row(
                    [
                        dbc.Col([x], width=6),
                        dbc.Col([y], width=6),
                    ]
                )
            )
        if len(sums_cards) % 2 != 0:
            rows.append(dbc.Row([dbc.Col([sums_cards[-1]], width=6)]))

        histogram_cards = telemetry_interface.create_histogram_cards()
        if histogram_cards:
            rows.append(html.H4("Histogram Metrics"))
        for x, y in zip(histogram_cards[::2], histogram_cards[1::2]):
            rows.append(
                dbc.Row(
                    [
                        dbc.Col([x], width=6),
                        dbc.Col([y], width=6),
                    ]
                )
            )
        if len(histogram_cards) % 2 != 0:
            rows.append(dbc.Row([dbc.Col([histogram_cards[-1]], width=6)]))

        return dbc.Row(rows)
