import asyncio
import os
from dataclasses import asdict

import dash
import pandas as pd
from dash import html, callback
from dash.dependencies import Input, Output
from temporalio.client import Client, WorkflowExecutionStatus
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import pygwalker as pyg
import dash_dangerously_set_inner_html

dash.register_page(__name__)


async def fetch_workflows_list():
    client = await Client.connect("0.0.0.0:7233")
    workflows = client.list_workflows()
    workflow_list = []
    async for workflow in workflows:
        workflow_list.append(asdict(workflow))
    return workflow_list


workflows_df = pd.DataFrame([])
layout = html.Div([
    dbc.Container([
        html.H1('Workflows Dashboard'),
        dbc.Button("Refresh", id="refresh-button", class_name="icon-refresh"),
        dag.AgGrid(
            id='workflows-table',
            columnDefs=[{"field": i} for i in workflows_df.columns],
            rowData=workflows_df.to_dict("records"),
            columnSize="sizeToFit",
            defaultColDef={
                "filter": True, "tooltipComponent": "CustomTooltipSimple"
            },
            dashGridOptions={
                "pagination": True, "animateRows": False,
                'tooltipShowDelay': 0, 'tooltipHideDelay': 2000,
                "rowSelection": "single",
            },
            className="ag-theme-balham compact dbc-ag-grid",
        ),
        html.Br(),
        html.H2("Workflow Analysis"),
        dbc.Row([
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader("Output Files"),
                    dbc.CardBody([
                    ], id="output-files")
                ])
            ], width=4),
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader("Analysis"),
                    dbc.CardBody([
                        html.Div(id="file-analysis")
                    ])
                ])
            ], width=8)
        ]),
        html.Br(),

        dbc.Accordion([
            dbc.AccordionItem(
                [
                    dag.AgGrid(
                        id='history-table',
                        columnDefs=[],
                        rowData=[],
                        columnSize="sizeToFit",
                        defaultColDef={
                            "filter": True
                        },
                        dashGridOptions={
                            "pagination": True, "animateRows": True,
                            'tooltipShowDelay': 0, 'tooltipHideDelay': 2000
                        },
                        className="ag-theme-balham compact dbc-ag-grid",
                    )
                ],
                title="Workflow History",
            )
        ])
    ],
        fluid=True,
        className="dbc"
    ),
])


@callback(
    [
        Output('workflows-table', 'rowData'),
        Output('workflows-table', 'columnDefs'),
    ],
    [Input('refresh-button', 'n_clicks')]
)
def refresh_workflows_table(n_clicks):
    workflow_list = asyncio.run(fetch_workflows_list(), debug=True)
    df = pd.DataFrame(workflow_list)
    del df["data_converter"]
    del df["raw_info"]
    del df["typed_search_attributes"]

    df["run_time"] = df["close_time"] - df["execution_time"]
    df["run_time"] = df["run_time"].dt.total_seconds()
    df["run_id"] = "[" + df['run_id'] + "](http://localhost:8233/namespaces/default/workflows/" + df["id"] + "/" + df["run_id"] + "/history)"
    df["status"] = df["status"].apply(lambda x: WorkflowExecutionStatus(x).name)

    df = df.filter([
        "id", "status", "run_id", "workflow_type", "run_time", "execution_time", "start_time", "close_time", "task_queue",
    ])

    column_defs = []
    for column in df.columns:
        if column == "run_id":
            column_defs.append({
                "field": column,
                "headerName": column,
                "linkTarget": "_blank",
                "cellRenderer": "markdown"
            })
        elif column == "status":
            column_defs.append({
                "field": column,
                "cellStyle": {
                    "styleConditions": [
                        {
                            "condition": f"params.value == '{WorkflowExecutionStatus.RUNNING.name}'",
                            "style": { "backgroundColor": "blue", "color": "white" }
                        },
                        {
                            "condition": f"params.value == '{WorkflowExecutionStatus.COMPLETED.name}'",
                            "style": { "backgroundColor": "green", "color": "white" }
                        },
                        {
                            "condition": f"params.value == '{WorkflowExecutionStatus.FAILED.name}'",
                            "style": { "backgroundColor": "red", "color": "white" }
                        }
                    ]
                }
            })
        else:
            column_defs.append({"field": column})
    return df.to_dict("records"), column_defs


async def fetch_history(workflow_id, run_id):
    client = await Client.connect("0.0.0.0:7233")
    history_obj = await client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history()
    return history_obj.to_json_dict()


@callback(
    Output('history-table', 'rowData'),
    Output('history-table', 'columnDefs'),
    [Input('workflows-table', 'selectedRows')]
)
def workflow_selected(selected_rows):
    if not selected_rows:
        return [], []
    workflow_id = selected_rows[0]["id"]
    run_id_markdown = selected_rows[0]["run_id"]
    run_id = run_id_markdown[1:run_id_markdown.index("]")]
    history = asyncio.run(fetch_history(workflow_id, run_id), debug=True)
    df = pd.DataFrame(history['events'])
    return df.to_dict("records"), [{"field": i} for i in df.columns]

@callback(
    Output("output-files", "children"),
    [Input('workflows-table', 'selectedRows')],
    prevent_initial_call=True
)
def file_listing(selected_rows):
    if not selected_rows:
        return []

    files = []
    workflow_id = selected_rows[0]["id"]
    run_id_markdown = selected_rows[0]["run_id"]
    run_id = run_id_markdown[1:run_id_markdown.index("]")]
    for root, dirs, filenames in os.walk(f"/tmp/dapr/objectstore/{workflow_id}/{run_id}"):
        if not filenames:
            continue

        file_dir = os.path.relpath(root, f"/tmp/dapr/objectstore/{workflow_id}/{run_id}")
        for filename in filenames:
            files.append({
                "dir": file_dir,
                "filename": filename,
                "full_path": os.path.join(root, filename)
            })
    files_df = pd.DataFrame(files)
    return [
        html.Div(f"Displaying files from /tmp/dapr/objectstore/{workflow_id}/{run_id}"),

        dag.AgGrid(
        id='files-table',
        columnDefs=[{"field": i} for i in files_df.columns],
        rowData=files_df.to_dict("records"),
        columnSize="sizeToFit",
        defaultColDef={
            "filter": True, "tooltipComponent": "CustomTooltipSimple"
        },
        dashGridOptions={
            "pagination": True, "animateRows": False,
            'tooltipShowDelay': 0, 'tooltipHideDelay': 2000,
            "rowSelection": "single",
        },
        className="ag-theme-balham compact dbc-ag-grid",
    )]

@callback(
    Output("file-analysis", "children"),
    [Input('files-table', 'selectedRows')],
    prevent_initial_call=True
)
def debug_file(selected_rows):
    if not selected_rows:
        return
    selected_file = selected_rows[0]
    if not selected_file['filename'].endswith(".json"):
        print("Not a JSON file")
        return

    df = None
    try:
        df = pd.read_json(selected_rows[0]["full_path"], lines=True)
    except Exception as e:
        print(e)
        pass

    if df is None:
        df = pd.read_json(selected_rows[0]["full_path"])

    walker = pyg.walk(df, debug=False)
    html_code = walker.to_html()
    return dash_dangerously_set_inner_html.DangerouslySetInnerHTML(html_code)