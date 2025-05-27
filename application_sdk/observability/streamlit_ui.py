import streamlit as st
import duckdb
import pandas as pd
import os
import logging
import plotly.express as px

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(page_title="Atlan Observability", layout="wide")

# Initialize DuckDB connection
try:
    con = duckdb.connect('observability.db')
    logger.info("Successfully connected to DuckDB")
except Exception as e:
    logger.error(f"Failed to connect to DuckDB: {e}")
    st.error(f"Failed to connect to database: {e}")

# Function to load data from parquet files
def load_data(data_type):
    try:
        # Get the absolute path to the observability directory
        current_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(current_dir, data_type)
        logger.info(f"Looking for data in directory: {data_dir}")
        
        if not os.path.exists(data_dir):
            logger.warning(f"Directory does not exist: {data_dir}")
            st.warning(f"No data directory found for {data_type}")
            return pd.DataFrame()
            
        # List all parquet files
        parquet_files = []
        for root, _, files in os.walk(data_dir):
            for file in files:
                if file.endswith('.parquet'):
                    parquet_files.append(os.path.join(root, file))
        
        if not parquet_files:
            logger.warning(f"No parquet files found in {data_dir}")
            st.warning(f"No data files found for {data_type}")
            return pd.DataFrame()
            
        logger.info(f"Found {len(parquet_files)} parquet files")
        
        # Read all parquet files
        query = f"""
        SELECT *
        FROM read_parquet(
            {parquet_files},
            hive_partitioning=true,
            hive_types={{'year': INTEGER, 'month': INTEGER, 'day': INTEGER}}
        )
        """
        df = con.execute(query).df()
        logger.info(f"Successfully loaded {len(df)} rows of {data_type} data")
        return df
        
    except Exception as e:
        logger.error(f"Error loading {data_type} data: {e}")
        st.error(f"Error loading {data_type} data: {e}")
        return pd.DataFrame()

# Sidebar for navigation
st.sidebar.title("Navigation")
page = st.sidebar.radio("Go to", ["Logs", "Metrics", "Traces"])

# Main content
st.title(f"Atlan Observability - {page}")

# Load and display data based on selection
if page == "Logs":
    df = load_data("logs")
    if not df.empty:
        st.write(f"Total records: {len(df)}")
        st.dataframe(df)
        
        # Create a stacked bar chart of log levels over time
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
        df['minute'] = df['timestamp'].dt.floor('min')
        
        # Create pivot table for stacked bar chart
        log_levels = df.pivot_table(
            index='minute',
            columns='level',
            values='timestamp',
            aggfunc='count',
            fill_value=0
        )
        
        # Plot stacked bar chart
        st.bar_chart(log_levels)
    else:
        st.info("No log data available")
elif page == "Metrics":
    df = load_data("metrics")
    if not df.empty:
        st.write(f"Total records: {len(df)}")
        
        # Convert timestamp to datetime
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
        
        # Separate counter and histogram metrics
        counter_metrics = df[df['type'] == 'counter']
        histogram_metrics = df[df['type'] == 'histogram']
        
        # Display counter metrics
        if not counter_metrics.empty:
            st.subheader("Counter Metrics")
            # Get unique counter metric names
            counter_names = counter_metrics['name'].unique()
            
            for metric_name in counter_names:
                metric_data = counter_metrics[counter_metrics['name'] == metric_name]
                # Sort by timestamp to ensure proper cumulative view
                metric_data = metric_data.sort_values('timestamp')
                # Create cumulative sum
                metric_data['cumulative_value'] = metric_data.groupby('name')['value'].cumsum()
                
                st.write(f"### {metric_name}")
                st.line_chart(metric_data.set_index('timestamp')['cumulative_value'])
        
        # Display histogram metrics
        if not histogram_metrics.empty:
            st.subheader("Histogram Metrics")
            # Get unique histogram metric names
            histogram_names = histogram_metrics['name'].unique()
            
            for metric_name in histogram_names:
                metric_data = histogram_metrics[histogram_metrics['name'] == metric_name]
                
                st.write(f"### {metric_name}")
                # Create histogram
                fig = px.histogram(
                    metric_data,
                    x='value',
                    nbins=50,
                    title=f"Distribution of {metric_name}"
                )
                st.plotly_chart(fig)
                
                # Show time series of mean values
                st.write(f"Mean {metric_name} over time")
                time_series = metric_data.groupby('timestamp')['value'].mean()
                st.line_chart(time_series)
    else:
        st.info("No metrics data available")
elif page == "Traces":
    df = load_data("traces")
    if not df.empty:
        st.write(f"Total records: {len(df)}")
        st.dataframe(df)
        # Group by timestamp and count records
        time_series = df.groupby(pd.to_datetime(df['timestamp'], unit='s').dt.floor('min')).size()
        st.line_chart(time_series)
    else:
        st.info("No traces data available") 