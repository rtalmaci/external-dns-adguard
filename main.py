import asyncio
import logging
import multiprocessing
import sys

import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from kubernetes import client, config, watch

from config import Config

load_dotenv()

session = requests.Session()
session.auth = (Config.ADGUARD_USER, Config.ADGUARD_PASS)

logger = logging.getLogger("external-dns-adguard")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

def ingress_event(netV1):
  watcher = watch.Watch()
  for event in watcher.stream(netV1.list_ingress_for_all_namespaces):
    if event["type"] == "ADDED" or event["type"] == "MODIFIED":
      dns_records_resp = session.get(f'http://{Config.ADGUARD_DNS}/control/rewrite/list')
      if dns_records_resp.ok:
        dns_records = dns_records_resp.json()
      else:
        logger.error("Couldnt reach adguard")
        pass
      missing_record = ""
      for rule in event["object"].spec.rules:
        if Config.DOMAIN_NAME in rule.host:
          # missing_record needs to be set outside of the below for loop
          # otherwise the user _has_ to have at least one record in
          # adguard
          missing_record = rule.host
          for record in dns_records:
            if rule.host in record["domain"]:
              missing_record = ""
              break
      if missing_record:
        try:
          lb_address = event["object"].status.load_balancer.ingress[0].ip
          logger.error(f'-- EVENT {event["type"]} -- Found loadbalancer IP: {lb_address}')
        except Exception as ex:
          logger.error(f'-- EVENT {event["type"]} -- Couldn\'t retrieve LoadBalancer address from record \'{missing_record}\'.')
          continue

        record = {
          "domain": missing_record,
          "answer" : lb_address
        }
        add_record_resp = session.post(f'http://{Config.ADGUARD_DNS}/control/rewrite/add', json=record)
        if add_record_resp.ok:
          logger.info(f'-- EVENT {event["type"]} -- {record["domain"]} has been added to DNS')
        else:
          logger.error(f'-- EVENT {event["type"]} -- Error occured while requesting to add {record["domain"]} to DNS')
          logger.error(f'-- EVENT {event["type"]} -- {add_record_resp.json()}')

def ingress_deletion(netV1, force_deletion):
  dns_records_resp = session.get(f'http://{Config.ADGUARD_DNS}/control/rewrite/list')
  if dns_records_resp.ok:
    dns_records = dns_records_resp.json()
  ingress_records = netV1.list_ingress_for_all_namespaces()
  ingress_list = []
  load_balancer_ip = ""
  for ingress in ingress_records.items:
    try:
      load_balancer_ip = ingress.status.load_balancer.ingress[0].ip
    except Exception as ex:
      if not force_deletion:
        logger.error(f'-- EVENT DELETE -- Couldn\'t retrieve LoadBalancer ingress from record \'{ingress.metadata.name}\'.')
      continue
    load_balancer_ip = ingress.status.load_balancer.ingress[0].ip
    for rule in ingress.spec.rules:
      ingress_list.append(rule.host)
  
  for record in dns_records:
    if record["domain"] not in ingress_list:
      if force_deletion:
        record_to_delete = {
          "answer": record["answer"],
          "domain": record["domain"]
        }

        deletion_response = session.post(f'http://{Config.ADGUARD_DNS}/control/rewrite/delete', json=record_to_delete)

        if deletion_response.ok:
          logger.info(f'-- EVENT DELETE -- {record["domain"]} has been deleted from DNS')
        else:
          logger.error(f'-- EVENT DELETE -- Error occured while requesting to delete {record["domain"]} from DNS')
          logger.error(f'-- EVENT DELETE -- {deletion_response.json()}')
      elif not force_deletion and record["answer"] == load_balancer_ip:
        record_to_delete = {
          "domain": record["domain"],
          "answer": record["answer"]
        }

        deletion_response = session.post(f'http://{Config.ADGUARD_DNS}/control/rewrite/delete', json=record_to_delete)
        if deletion_response.ok:
          logger.info(f'-- EVENT DELETE -- {record["domain"]} has been deleted from DNS')
        else:
          logger.error(f'-- EVENT DELETE -- Error occured while requesting to delete {record["domain"]} from DNS')
          logger.error(f'-- EVENT DELETE -- {deletion_response.json()}')

if __name__ == "__main__":
  for key,val in Config.__dict__.items():
    if not str(key).startswith("__"):
      if val == None or val == "":
        logger.error(f'Environment Variable {key} is not set. Exiting')
        sys.exit(0)

  force_deletion = False

  for flag in sys.argv[1:]:
    if "-f" == flag or "--force" == flag:
      force_deletion = True

  ioloop = asyncio.get_event_loop()
  try:
    if Config.MODE == "DEV":
      config.load_kube_config()
    elif Config.MODE == "PROD":
      config.load_incluster_config()
    else:
      logger.error("Incorrect MODE type")
      sys.exit(0)

    logger.info("Starting application")

    netV1 = client.NetworkingV1Api()

    proc = multiprocessing.Process(target=ingress_event, args=[netV1])
    proc.start()
    proc.join(5)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(ingress_deletion, 'interval', [netV1, force_deletion], seconds=15)
    scheduler.start()

    ioloop.run_forever()
  except (KeyboardInterrupt, SystemExit):
    pass
  finally:
    logger.info("Interrupt Received")
    proc.terminate()
    ioloop.close()