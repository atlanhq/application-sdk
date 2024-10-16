from dash import html
import dash
import dash_bootstrap_components as dbc

dash.register_page(__name__, name='🚀Deployment')

layout = html.Div([
    dbc.Container([
        html.H1("Deployment"),
        dbc.Card(
            [
                dbc.CardBody([
                    html.Div("Deploy settings go here")
                ])
            ]
        )
    ], fluid=True)
])
