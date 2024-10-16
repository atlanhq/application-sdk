from dash import html
import dash
import dash_bootstrap_components as dbc

dash.register_page(__name__)

layout = html.Div([
    dbc.Container([
        html.H1("Settings"),
        dbc.Card(
            [
                dbc.CardBody([
                    html.Div("Settings go here")
                ])
            ]
        )
    ], fluid=True)
])
