import dash
import dash_bootstrap_components as dbc
import pandas as pd
from dash import Input, Output, callback, html
from sqlalchemy import create_engine

from ui.pages.interfaces.utils import create_ag_grid

dash.register_page(__name__, name="🔍 Explore")

layout = html.Div(
    [
        dbc.Container(
            [
                html.H1("🔍 Source Exploration [WIP]"),
                dbc.Card(
                    [
                        dbc.CardHeader("SQL Source Exploration"),
                        dbc.CardBody(
                            [
                                dbc.Label("SQLAlchemy Connection String"),
                                dbc.Input(
                                    id="sqlalchemy_string",
                                    placeholder="postgresql+psycopg2://scott:tiger@localhost:5432/mydatabase",
                                    type="text",
                                ),
                                html.Br(),
                                dbc.Label("SQL Query"),
                                dbc.Input(
                                    id="sql_query",
                                    placeholder="SELECT * FROM my_table",
                                    type="text",
                                ),
                                dbc.ButtonGroup(
                                    [
                                        dbc.Button(
                                            "Run Query",
                                            outline=True,
                                            id="run_query",
                                            color="primary",
                                        ),
                                        dbc.Button(
                                            "AI Improve",
                                            outline=True,
                                            id="ai_improve",
                                            color="primary",
                                        ),
                                        dbc.Button(
                                            "Import Test Data",
                                            outline=True,
                                            id="import_test_data",
                                            color="primary",
                                        ),
                                        dbc.Button(
                                            "Import Scale Data",
                                            outline=True,
                                            id="import_scale_data",
                                            color="primary",
                                        ),
                                    ]
                                ),
                                html.Br(),
                                create_ag_grid(grid_id="sql_table", row_df=None),
                            ]
                        ),
                    ]
                ),
            ],
            fluid=True,
        )
    ]
)


@callback(
    [Output("sql_table", "rowData"), Output("sql_table", "columnDefs")],
    [
        Input("run_query", "n_clicks"),
        Input("sqlalchemy_string", "value"),
    ],
    prevent_initial_call=True,
)
def run_sql_query(n_clicks, sqlalchemy_string):
    if n_clicks is None:
        return dash.no_update
    conn = create_engine(sqlalchemy_string)
    df = pd.read_sql_query(sqlalchemy_string, conn)
    return df.to_dict("records"), [{"field": i} for i in df.columns]
