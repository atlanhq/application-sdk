from dash import Dash, html, dcc
import dash_bootstrap_components as dbc
import dash

dbc_css = "https://cdn.jsdelivr.net/gh/AnnMarieW/dash-bootstrap-templates/dbc.min.css"
app = Dash(__name__, use_pages=True, assets_folder="assets", external_stylesheets=[dbc.themes.COSMO, dbc_css])
app.title = "Atlan Application Dashboard"

app.layout = html.Div([
    dbc.Container([
        html.H1('Atlan Application Dashboard', className="p-2 mb-2 text-center"),
        dbc.Nav(
            [
                dbc.NavItem(dbc.NavLink(
                    page['name'],
                    href=page['relative_path'],
                    active="exact"
                )
                ) for page in dash.page_registry.values()
            ],
            pills=True, justified=True
        ),
        dash.page_container
    ], fluid=True)
])

if __name__ == '__main__':
    app.run(debug=False)
