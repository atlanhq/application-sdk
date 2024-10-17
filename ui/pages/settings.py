import dash
import dash_bootstrap_components as dbc
from dash import html

dash.register_page(__name__, name="⚙️Settings")

layout = html.Div(
    [
        dbc.Container(
            [
                html.H1("Settings"),
                dbc.Card([dbc.CardBody([html.Div("Settings go here")])]),
            ],
            fluid=True,
        )
    ]
)
