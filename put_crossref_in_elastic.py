import os
import sickle
import boto
import datetime
import requests
from time import sleep
from time import time
from urllib import quote
import zlib
import re
import json
import argparse
from util import JSONSerializerPython2
from elasticsearch import Elasticsearch, RequestsHttpConnection, compat, exceptions
from elasticsearch.helpers import parallel_bulk
from elasticsearch.helpers import bulk

from util import elapsed

# set up elasticsearch
INDEX_NAME = "crossref"
TYPE_NAME = "crosserf_api"  #TYPO!!!  but i think for now we run with it???


# data from https://archive.org/details/crossref_doi_metadata
# To update the dump, use the public API with deep paging:
# http://api.crossref.org/works?filter=from-update-date:2016-04-01&rows=1000&cursor=*
# The documentation for this feature is available at:
# https://github.com/CrossRef/rest-api-doc/blob/master/rest_api.md#deep-paging-with-cursors


def is_good_file(filename):
    return "chunk_" in filename

def set_up_elastic(url=None):
    if not url:
        url = os.getenv("CROSSREF_ES_URL")
    es = Elasticsearch(url,
                       serializer=JSONSerializerPython2(),
                       retry_on_timeout=True,
                       max_retries=100)

    # if es.indices.exists(INDEX_NAME):
    #     print("deleting '%s' index..." % (INDEX_NAME))
    #     res = es.indices.delete(index = INDEX_NAME)
    #     print(" response: '%s'" % (res))
    #
    # # print u"creating index"
    # mapping = {
    #   "mappings": {
    #     TYPE_NAME: {
    #         "doi": { "type": "string", "index": "not_analyzed" }
    #     }
    #   }
    # }
    #
    # res = es.indices.create(index=INDEX_NAME, ignore=400, body=mapping)
    return es


def make_record_for_es(record):
    action_record = record
    action_record.update({
        '_op_type': 'index',
        '_index': INDEX_NAME,
        '_type': TYPE_NAME,
        '_id': record["doi"]})
    return action_record


def save_records_in_es(es, records_to_save, threads, chunk_size):
    print "starting save"
    start_time = time()

    # have to do call parallel_bulk in a for loop because is parallel_bulk is a generator so you have to call it to
    # have it do the work.  see https://discuss.elastic.co/t/helpers-parallel-bulk-in-python-not-working/39498
    if threads > 1:
        for success, info in parallel_bulk(es,
                                           actions=records_to_save,
                                           refresh=False,
                                           request_timeout=60,
                                           thread_count=threads,
                                           chunk_size=chunk_size):
            if not success:
                print('A document failed:', info)
    else:
        for success_info in bulk(es, actions=records_to_save, refresh=False, request_timeout=60, chunk_size=chunk_size):
            pass
    print u"done sending {} records to elastic in {}s".format(len(records_to_save), elapsed(start_time, 4))


def get_citeproc_date(year=0, month=1, day=1):
    try:
        return datetime.datetime(year, month, day).isoformat()
    except ValueError:
        return None


def build_crossref_record(data):
    record = {}

    simple_fields = [
        "publisher",
        "subject",
        "link",
        "license",
        "funder",
        "type",
        "update-to",
        "clinical-trial-number",
        "issn",
        "isbn",
        "alternative-id"
    ]

    for field in simple_fields:
        if field in data:
            record[field.lower()] = data[field]

    if "title" in data:
        if isinstance(data["title"], basestring):
            record["title"] = data["title"]
        else:
            if data["title"]:
                record["title"] = data["title"][0]  # first one
        if "title" in record and record["title"]:
            record["title"] = re.sub(u"\s+", u" ", record["title"])


    if "container-title" in data:
        record["all_journals"] = data["container-title"]
        if isinstance(data["container-title"], basestring):
            record["journal"] = data["container-title"]
        else:
            if data["container-title"]:
                record["journal"] = data["container-title"][-1] # last one

    if "author" in data:
        # record["authors_json"] = json.dumps(data["author"])
        record["all_authors"] = data["author"]
        if data["author"]:
            first_author = data["author"][0]
            if first_author and u"family" in first_author:
                record["first_author_lastname"] = first_author["family"]
            for author in record["all_authors"]:
                if author and "affiliation" in author and not author.get("affiliation", None):
                    del author["affiliation"]


    if "issued" in data:
        # record["issued_raw"] = data["issued"]
        try:
            if "raw" in data["issued"]:
                record["year"] = int(data["issued"]["raw"])
            elif "date-parts" in data["issued"]:
                record["year"] = int(data["issued"]["date-parts"][0][0])
                date_parts = data["issued"]["date-parts"][0]
                pubdate = get_citeproc_date(*date_parts)
                if pubdate:
                    record["pubdate"] = pubdate
        except (IndexError, TypeError):
            pass

    if "deposited" in data:
        try:
            record["deposited"] = data["deposited"]["date-time"]
        except (IndexError, TypeError):
            pass


    record["added_timestamp"] = datetime.datetime.utcnow().isoformat()
    return record



def s3_to_elastic(first=None, last=None, url=None, threads=0, chunk_size=None):
    es = set_up_elastic(url)

    # set up aws s3 connection
    conn = boto.connect_s3(
        os.getenv("AWS_ACCESS_KEY_ID"),
        os.getenv("AWS_SECRET_ACCESS_KEY")
    )

    my_bucket = conn.get_bucket('impactstory-crossref')

    i = 0
    records_to_save = []

    keys = my_bucket.list()

    for key in keys:
        if not is_good_file(key.name):
            continue

        key_filename = key.name.split("/")[-1]  # get rid of all subfolders

        if first and key_filename < first:
            continue

        if last and key_filename > last:
            continue

        print "getting this key...", key.name
        contents = key.get_contents_as_string()

        # fd = open("/Users/hpiwowar/Downloads/chunk_0000", "r")
        # contents = fd.read

        for line in contents.split("\n"):
            # print ":",
            if not line:
                continue

            (doi, data_date, data_text) = line.split("\t")
            data = json.loads(data_text)

            # make sure this is unanalyzed
            record = build_crossref_record(data)
            record["doi"] = doi.lower()
            action_record = make_record_for_es(record)
            records_to_save.append(action_record)

        i += 1
        if len(records_to_save) >= 1:  #10000
            save_records_in_es(es, records_to_save, threads, chunk_size)
            records_to_save = []

        print "at bottom of loop"

    # make sure to get the last ones
    print "saving last ones"
    save_records_in_es(es, records_to_save, 1, chunk_size)
    print "done everything"






def api_to_elastic(first=None, last=None, threads=0, chunk_size=None):
    es = set_up_elastic()
    i = 0
    records_to_save = []

    headers={"Accept": "application/json", "User-Agent": "impactstory.org"}

    base_url_with_last = "http://api.crossref.org/works?filter=from-created-date:{first},until-created-date:{last}&rows=1000&cursor={next_cursor}"
    base_url_no_last = "http://api.crossref.org/works?filter=from-created-date:{first}&rows=1000&cursor={next_cursor}"

    # but if want all changes, use "indexed" not "created" as per https://github.com/CrossRef/rest-api-doc/blob/master/rest_api.md#notes-on-incremental-metadata-updates
    # base_url_with_last = "http://api.crossref.org/works?filter=from-indexed-date:{first},until-indexed-date:{last}&rows=1000&cursor={next_cursor}"
    # base_url_no_last = "http://api.crossref.org/works?filter=from-indexed-date:{first}&rows=1000&cursor={next_cursor}"

    next_cursor = "*"
    has_more_responses = True
    if not first:
        first = "2016-04-01"

    while has_more_responses:
        if last:
            url = base_url_with_last.format(first=first, last=last, next_cursor=next_cursor)
        else:
            # query is much faster if don't have a last specified, even if it is far in the future
            url = base_url_no_last.format(first=first, next_cursor=next_cursor)

        print url
        start_time = time()
        resp = requests.get(url, headers=headers)
        print "getting crossref response took {}s".format(elapsed(start_time, 2))
        if resp.status_code != 200:
            print u"error in crossref call, status_code = {}".format(resp.status_code)
            return

        resp_data = resp.json()["message"]
        next_cursor = quote(resp_data["next-cursor"])
        if not resp_data["items"]:
            has_more_responses = False

        for data in resp_data["items"]:
            # print ":",
            record = build_crossref_record(data)
            doi = data["DOI"].lower()
            record["doi"] = doi

            action_record = make_record_for_es(record)
            records_to_save.append(action_record)

        i += 1
        if len(records_to_save) >= 1:  #10000
            save_records_in_es(es, records_to_save, threads, chunk_size)
            print "last deposted date", records_to_save[-1]["deposited"]
            records_to_save = []

        print "at bottom of loop"

    # make sure to get the last ones
    print "saving last ones"
    save_records_in_es(es, records_to_save, 1, chunk_size)
    print "done everything"








if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run stuff.")

    # just for updating lots
    # function = s3_to_elastic
    # parser.add_argument('--first', nargs="?", type=str, help="first filename to process (example: --first --first chunk_0012")
    # parser.add_argument('--last', nargs="?", type=str, help="last filename to process (example: --last chunk_0012)")

    function = api_to_elastic
    parser.add_argument('--first', nargs="?", type=str, help="first filename to process (example: --first 2006-01-01")
    parser.add_argument('--last', nargs="?", type=str, help="last filename to process (example: --last 2006-01-01)")

    # for both
    parser.add_argument('--threads', nargs="?", type=int, help="how many threads if multi")
    parser.add_argument('--chunk_size', nargs="?", type=int, default=100, help="how many docs to put in each POST request")


    parsed = parser.parse_args()

    print u"calling {} with these args: {}".format(function.__name__, vars(parsed))
    function(**vars(parsed))



# this gets things by doi

# GET /_search
# {
#   "query": {
#     "simple_query_string" : {
#         "query": "10.1103/physrevb.89.064510",
#         "fields": ["doi"],
#         "default_operator": "and"
#     }
#   }
# }
