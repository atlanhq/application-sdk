import dash_ag_grid as dag
import pandas as pd


def create_ag_grid(
    grid_id: str,
    row_df: pd.DataFrame = None,
    column_defs=None,
    default_col_def=None,
    grid_options=None,
) -> dag.AgGrid:
    if row_df is None:
        row_df = pd.DataFrame([])
    if column_defs is None:
        column_defs = [{"field": i} for i in row_df.columns]

    if default_col_def is None:
        default_col_def = {
            "filter": True,
            "tooltipComponent": "CustomTooltipSimple",
        }

    if grid_options is None:
        grid_options = {
            "pagination": True,
            "animateRows": False,
            "tooltipShowDelay": 0,
            "tooltipHideDelay": 2000,
            "rowSelection": "single",
        }

    return dag.AgGrid(
        id=grid_id,
        columnDefs=column_defs,
        rowData=row_df.to_dict("records"),
        columnSize="sizeToFit",
        defaultColDef=default_col_def,
        dashGridOptions=grid_options,
        className="ag-theme-balham compact dbc-ag-grid",
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
