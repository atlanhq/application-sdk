from dash import Dash, html, dcc
import dash_bootstrap_components as dbc
import dash

dbc_css = "https://cdn.jsdelivr.net/gh/AnnMarieW/dash-bootstrap-templates/dbc.min.css"
app = Dash(__name__, use_pages=True, assets_folder="assets", external_stylesheets=[dbc.themes.COSMO, dbc_css])
app.title = "Atlan Application Dashboard"

app.layout = html.Div([
    dbc.NavbarSimple(
        children=[
            dbc.NavItem(dbc.NavLink(page['name'], href=page['relative_path']))
            for page in dash.page_registry.values()
        ],
        brand="Atlan Application Dashboard",
        brand_href="#",
        color="primary",
        dark=True,
        sticky="top",
    ),
    dash.page_container
])

if __name__ == '__main__':
    app.run(debug=True)
