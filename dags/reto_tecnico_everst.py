from airflow.operators.python_operator import PythonOperator
from airflow.operators.bash_operator import BashOperator
from airflow.exceptions import AirflowFailException
from airflow.models import Variable
from airflow import DAG
from urllib.request import Request, urlopen
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import logging
import gzip
import json
import os

start_date = datetime(2023, 1, 1)

# Variables
url = Variable.get("url")

dag_id = f"reto_tecnico_everst"
default_dag_args = {
    'start_date': start_date,
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 0,
    'retry_delay': timedelta(seconds=10)
}


def download_data(url, **kwargs):
    """Función para descargar el contenido del API y guardarlo en RAW"""
    task_instance = kwargs['ti']
    try:
        request_site = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        gz_content = urlopen(request_site).read()
    except Exception as e:
        logging.error(f'Se genero un Error al intentar conectar con la url: {url}')
        raise AirflowFailException(e)
    logging.info('Generando descarga del GZ')
    json_content = gzip.decompress(gz_content)
    data = json.loads(json_content)
    now = datetime.now()
    dt_string = now.strftime("%Y%m%d_%H.%M")
    json_path = f'data/Raw/base_api/base_{dt_string}.json'
    task_instance.xcom_push(key="json_path", value=json_path)
    logging.info(f'Guardando la base generada desde el API, path: {json_path}')
    with open(json_path, 'w') as file:
        json.dump(data,file)


def table_by_municipio(task_xcom, **kwargs):
    """Función para crear la tabla por municipio"""
    task_instance = kwargs['ti']
    json_path = task_instance.xcom_pull(task_ids=task_xcom, key="json_path")
    logging.info('Leyendo la base extraida del API')
    with open(json_path, 'r') as file:
        data = json.loads(file.read())
    df = pd.DataFrame(data)
    date_lst = list(df['hloc'].unique())
    date_lst.sort(reverse=True)
    n_df = df[df['hloc'] >= date_lst[1]]
    logging.info('Formateando los tipos de datos en el DataFrame.')
    n_df['temp'] = pd.to_numeric(n_df["temp"])
    n_df['prec'] = pd.to_numeric(n_df["prec"])
    n_df['ides'] = n_df['ides'].astype(np.int64)
    n_df['idmun'] = n_df['idmun'].astype(np.int64)
    logging.info('Creando el promedio de temperatura y precipitación de las últimas dos horas.')
    n_df = n_df.groupby(['ides', 'idmun', 'nes', 'nmun'])['prec', 'temp'].agg('mean').reset_index()
    now = datetime.now()
    dt_string = now.strftime("%Y%m%d_%H.%M")
    parquet_path = f'data/Staging/base_2/base_{dt_string}.parquet'
    task_instance.xcom_push(key="parquet_path_tbl_by_mun", value=parquet_path)
    logging.info(f'Guardando la base tabla por municipio en formato parquet, path: {parquet_path}')
    # Saving as parquet
    n_df.to_parquet(parquet_path)


def get_last_data_municipios(**kwargs):
    """Función para crear la tabla data_municipio"""
    folder = 'data/Raw/data_municipios'
    task_instance = kwargs['ti']
    max_dt_mun = max(os.listdir(folder))
    file = os.listdir(f'{folder}/{max_dt_mun}/')[0]
    path_max_dt_mun = f'{folder}/{max_dt_mun}/{file}'
    task_instance.xcom_push(key="path_max_dt_mun", value=path_max_dt_mun)
    logging.info(f'Generando el path del archivo mas reciente de data_municipios: {path_max_dt_mun}')


def cruzar_base2_datamunicipios(task_base_2, taskmun, **kwargs):
    """Función para cruzar los datos generaos por base_2 y data_municipios"""
    task_instance = kwargs['ti']
    base_2_path = task_instance.xcom_pull(task_ids=task_base_2, key="parquet_path_tbl_by_mun")
    dt_mun_path = task_instance.xcom_pull(task_ids=taskmun, key="path_max_dt_mun")
    df_base_2 = pd.read_parquet(base_2_path, engine='pyarrow')
    df_dt_mun = pd.read_csv(dt_mun_path, sep=',')
    df_dt_mun.columns = ['ides', 'idmun', 'value']
    df_base_3 = pd.merge(left=df_base_2, right=df_dt_mun, how='left', on=['ides', 'idmun'])
    now = datetime.now()
    dt_string = now.strftime("%Y%m%d_%H.%M")
    parquet_path = f'data/Analytics/base_3/History/base_3_{dt_string}.parquet'
    task_instance.xcom_push(key="parquet_base3", value=parquet_path)
    logging.info(f'Guardando la base del cruce de data_municipios y base_02 en formato parquet, path: {parquet_path}')
    # Saving as parquet
    df_base_3.to_parquet(parquet_path)


with DAG(dag_id=dag_id, schedule_interval='0 */1 * * *', default_args=default_dag_args, max_active_runs=1, catchup=False) as dag:

    download_data = PythonOperator(
                                    task_id = "download_data",
                                    python_callable = download_data,
                                    op_args = [url],
                                    provide_context=True
                                )
    
    table_by_municipio = PythonOperator(
                                    task_id = "table_by_municipio",
                                    python_callable = table_by_municipio,
                                    op_args = ["download_data"],
                                    provide_context=True
                                )
    
    get_last_data_municipios = PythonOperator(
                                    task_id = "get_last_data_municipios",
                                    python_callable = get_last_data_municipios,
                                    provide_context=True
                                )
    
    cruzar_base2_datamunicipios = PythonOperator(
                                    task_id = "cruzar_base2_datamunicipios",
                                    python_callable = cruzar_base2_datamunicipios,
                                    op_args = ["table_by_municipio", "get_last_data_municipios"],
                                    provide_context=True
                                )
    
    create_current_base3_copy = BashOperator(
                                    task_id = "create_current_base3_copy",
                                    bash_command = "cp /opt/airflow/{{ task_instance.xcom_pull(task_ids='cruzar_base2_datamunicipios', key='parquet_base3') }} /opt/airflow/data/Analytics/base_3/base_3_current.parquet"
                                )

download_data >> table_by_municipio
[get_last_data_municipios, table_by_municipio] >> cruzar_base2_datamunicipios >> create_current_base3_copy