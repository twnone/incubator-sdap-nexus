# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import threading
import time
from datetime import datetime
from pytz import timezone, UTC

import requests
import pysolr
from shapely import wkt
from elasticsearch import Elasticsearch


ELASTICSEARCH_CON_LOCK = threading.Lock()
thread_local = threading.local()

EPOCH = timezone('UTC').localize(datetime(1970, 1, 1))
ELASTICSEARCH_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
ISO_8601 = '%Y-%m-%dT%H:%M:%S%z'


class ElasticsearchProxy(object):
    def __init__(self, config):
        self.elasticsearchHosts = config.get("elasticsearch", "host").split(',')
        self.elasticsearchIndex = config.get("elasticsearch", "index")
        self.elasticsearchUsername = config.get("elasticsearch", "username")
        self.elasticsearchPassword = config.get("elasticsearch", "password")
        self.logger = logging.getLogger(__name__)

        with ELASTICSEARCH_CON_LOCK:
            elasticsearchcon = getattr(thread_local, 'elasticsearchcon', None)
            if elasticsearchcon is None:
                elasticsearchcon = Elasticsearch(hosts=self.elasticsearchHosts, http_auth=(self.elasticsearchUsername, self.elasticsearchPassword))
                thread_local.elasticsearchcon = elasticsearchcon

            self.elasticsearchcon = elasticsearchcon

    def find_tile_by_id(self, tile_id):

        search = 'id:%s' % tile_id

        params = {
            'rows': 1
        }

        results, start, found = self.do_query(*(search, None, None, True, None), **params)

        assert len(results) == 1, "Found %s results, expected exactly 1" % len(results)
        return [results[0]]

    def find_tiles_by_id(self, tile_ids, ds=None, **kwargs):

        if ds is not None:
            search = 'dataset_s:%s' % ds
        else:
            search = '*:*'

        additionalparams = {
            'fq': [
                "{!terms f=id}%s" % ','.join(tile_ids)
            ]
        }

        self._merge_kwargs(additionalparams, **kwargs)

        results = self.do_query_all(*(search, None, None, False, None), **additionalparams)

        assert len(results) == len(tile_ids), "Found %s results, expected exactly %s" % (len(results), len(tile_ids))
        return results

    def find_min_date_from_tiles(self, tile_ids, ds=None, **kwargs):

        #if ds is not None:
        #    search = ds
        #else:
        #    search = ""
        search = "nexustiles"
        additionalparams = {
            "size": 0,
            "query": { 
                "bool": {
                    "filter": [
                        { "match": {
                            "dataset_s": ds
                        } }
                    ]
                }
            },
            "aggs": {
                "min_date_agg": {
                    "min": {
                        "field": "tile_min_time_dt"
                    }
                }
            }            
        }
        response = self.do_query_raw(*(search, None, None, True, None), **additionalparams)
        return self.convert_iso_to_datetime(response["aggregations"]['min_date_agg']["value_as_string"])


    def find_max_date_from_tiles(self, tile_ids, ds=None, **kwargs):

        #TODO : change hardcoded index name :
        search = "nexustiles"
        additionalparams = {
            "size": 0,
            "query": { 
                "bool": {
                    "filter": [
                        { "match": {
                            "dataset_s": ds
                        } }
                    ]
                }
            },
            "aggs": {
                "max_date_agg": {
                    "max": {
                        "field": "tile_max_time_dt"
                    }
                }
            }            
        }
        response = self.do_query_raw(*(search, None, None, True, None), **additionalparams)
        return self.convert_iso_to_datetime(response["aggregations"]['max_date_agg']["value_as_string"])


    def find_min_max_date_from_granule(self, ds, granule_name, **kwargs):
        search = 'dataset_s:%s' % ds

        kwargs['rows'] = 1
        kwargs['fl'] = 'tile_min_time_dt'
        kwargs['sort'] = ['tile_min_time_dt asc']
        additionalparams = {
            'fq': [
                "granule_s:%s" % granule_name
            ]
        }

        self._merge_kwargs(additionalparams, **kwargs)
        results, start, found = self.do_query(*(search, None, None, False, None), **additionalparams)
        start_time = self.convert_iso_to_datetime(results[0]['tile_min_time_dt'])

        kwargs['fl'] = 'tile_max_time_dt'
        kwargs['sort'] = ['tile_max_time_dt desc']
        additionalparams = {
            'fq': [
                "granule_s:%s" % granule_name
            ]
        }

        self._merge_kwargs(additionalparams, **kwargs)
        results, start, found = self.do_query(*(search, None, None, False, None), **additionalparams)
        end_time = self.convert_iso_to_datetime(results[0]['tile_max_time_dt'])

        return start_time, end_time

    def get_data_series_list(self):

        datasets = self.get_data_series_list_simple()

        for dataset in datasets:
            min_date = self.find_min_date_from_tiles([], ds=dataset['title'])
            max_date = self.find_max_date_from_tiles([], ds=dataset['title'])
            #dataset['start'] = min_date
            #dataset['end'] = max_date
            dataset['start'] = (min_date - EPOCH).total_seconds()
            dataset['end'] = (max_date - EPOCH).total_seconds()
            dataset['iso_start'] = min_date.strftime(ISO_8601)
            dataset['iso_end'] = max_date.strftime(ISO_8601)

        return datasets

    def get_data_series_list_simple(self):
        #search = "*:*"
        search = "nexustiles"
        params = {
            'size': 0,
            "aggs": {
                "dataset_list_agg": {
                    "terms": {
                        "size":100,
                        "field": "dataset_s"
                    }
                }
            }
        }

        response = self.do_query_raw(*(search, None, None, False, None), **params)
        self.logger.info(response)
        l = []

        for dataset in response["aggregations"]["dataset_list_agg"]["buckets"]:
            l.append({
                "shortName": dataset["key"],
                "title": dataset["key"],
                "tileCount": dataset["doc_count"]
            })

        l = sorted(l, key=lambda entry: entry["title"])
        return l

    def get_data_series_stats(self, ds):
        #TODO : change hardcoded index name :
        search = "nexustiles"

        params = {
            "size": 0,
            "_source": "tile_max_time_dt",
            "query": {
                "bool": {
                    "filter": [
                        { "match": {
                            "dataset_s": ds
                        } }
                    ]
                }
            },
            "aggs": {
                "terms_tile_max_time_dt": {
                    "terms": {
                        "size":10000,
                        "field": "tile_max_time_dt"
                    }
                },
                "min_tile_min_val_d": {
                    "min": {
                        "field": "tile_min_val_d"
                    }
                },
                "min_tile_max_time_dt": {
                    "min": {
                        "field": "tile_max_time_dt"
                    }
                },
                "max_tile_max_time_dt": {
                    "max": {
                        "field": "tile_max_time_dt"
                    }
                },
                "max_tile_max_val_d": {
                    "max": {
                        "field": "tile_max_val_d"
                    }
                }
            }
        }

#        params = {
#            "facet": "true",
#            "facet.field": ["dataset_s", "tile_max_time_dt"],
#            "facet.limit": "-1",
#            "facet.pivot": "{!stats=piv1}dataset_s",
#            "stats": "on",
#            "stats.field": ["{!tag=piv1 min=true max=true sum=false}tile_max_time_dt","{!tag=piv1 min=true max=false sum=false}tile_min_val_d","{!tag=piv1 min=false max=true sum=false}tile_max_val_d"]
#        }

        response = self.do_query_raw(*(search, None, None, False, None), **params)

        #self.logger.info("get_data_series_stats :: query response = {}".format(response))

        stats = {}
        stats["start"] = int(response["aggregations"]["min_tile_max_time_dt"]["value"]) / 1000
        stats["end"] = int(response["aggregations"]["max_tile_max_time_dt"]["value"]) / 1000
        stats["minValue"] = response["aggregations"]["min_tile_min_val_d"]["value"]
        stats["maxValue"] = response["aggregations"]["max_tile_max_val_d"]["value"]

#        for g in response.facets["facet_pivot"]["dataset_s"]:
#            if g["value"] == ds:
#                stats["start"] = self.convert_iso_to_timestamp(g["stats"]["stats_fields"]["tile_max_time_dt"]["min"])
#                stats["end"] = self.convert_iso_to_timestamp(g["stats"]["stats_fields"]["tile_max_time_dt"]["max"])
#                stats["minValue"] = g["stats"]["stats_fields"]["tile_min_val_d"]["min"]
#                stats["maxValue"] = g["stats"]["stats_fields"]["tile_max_val_d"]["max"]


        stats["availableDates"] = []
        for dt in response["aggregations"]["terms_tile_max_time_dt"]["buckets"]:
            stats["availableDates"].append(dt["key"] / 1000)
        #for dt in response["facet_fields"]["tile_max_time_dt"][::2]:
        #    stats["availableDates"].append(self.convert_iso_to_timestamp(dt))

        stats["availableDates"] = sorted(stats["availableDates"])

        #self.logger.info("get_data_series_stats :: stats = {}".format(stats))

        return stats

    def find_tile_by_polygon_and_most_recent_day_of_year(self, bounding_polygon, ds, day_of_year):

        #search = 'dataset_s:%s' % ds
        search = "nexustiles"

        params = {
            'fq': [
                "{!field f=geo}Intersects(%s)" % bounding_polygon.wkt,
                "tile_count_i:[1 TO *]",
                "day_of_year_i:[* TO %s]" % day_of_year
            ],
            'rows': 1
        }

        results, start, found = self.do_query(
            *(search, None, None, True, ('day_of_year_i desc',)), **params)

        return [results[0]]

    def find_days_in_range_asc(self, min_lat, max_lat, min_lon, max_lon, ds, start_time, end_time, **kwargs):

        #TODO : change hardcoded index name :
        search = "nexustiles"

        self.logger.info("min_lat : {}".format(min_lat))
        self.logger.info("max_lat : {}".format(max_lat))
        self.logger.info("min_lon : {}".format(min_lon))
        self.logger.info("max_lon : {}".format(max_lon))
        self.logger.info("start_time : {}".format(start_time))
        self.logger.info("end_time : {}".format(end_time))

        #self.logger.info("KWARGS :")
        #self.logger.info(kwargs)

        search_start_s = datetime.utcfromtimestamp(start_time).strftime(ELASTICSEARCH_FORMAT)
        search_end_s = datetime.utcfromtimestamp(end_time).strftime(ELASTICSEARCH_FORMAT)

        self.logger.info("start_time : {}".format(search_start_s))
        self.logger.info("end_time : {}".format(search_end_s))

        additionalparams = {
            "size": "0",
            "_source": "tile_min_time_dt",
            "query": {
                "bool": {
                    "filter": [
                        {
                            "match": {
                                "dataset_s": ds
                            }
                        },
                        {
                            "range": {
                                "tile_min_time_dt": {
                                    "gte": search_start_s,
                                    "lte": search_end_s
                                }
                            }
                        },
                        {
                            "geo_shape": {
                                "geo": {
                                    "shape": {
                                        "type": "envelope",
                                        "coordinates": [[min_lon, max_lat],[max_lon, min_lat]]
                                    },
                                    "relation": "within"
                                }
                            }
                        }
                    ]
                }
            },
            "aggs": {
                "days_range_agg": {
                    "terms": {
                        "size":10000,
                        "field": "tile_min_time_dt"
                    }
                }
            }
        }


#        additionalparams = {
#            'fq': [
#                "geo:[%s,%s TO %s,%s]" % (min_lat, min_lon, max_lat, max_lon),
#                "{!frange l=0 u=0}ms(tile_min_time_dt,tile_max_time_dt)",
#                "tile_count_i:[1 TO *]",
#                "tile_min_time_dt:[%s TO %s] " % (search_start_s, search_end_s)
#            ],
#            'rows': 0,
#            'facet': 'true',
#            'facet.field': 'tile_min_time_dt',
#            'facet.limit': '-1'
#        }

        #self._merge_kwargs(additionalparams, **kwargs)

        #response = self.do_query_raw(*(search, "scroll", None, False, None), **additionalparams)
        response = self.do_query_raw(*(search, None, None, False, None), **additionalparams)
        #self.logger.info(response)

        self.logger.info("DAYS RANGE : {}".format(response))

        results = [res["key"] for res in response["aggregations"]["days_range_agg"]["buckets"]]
        #scroll_id = response["_scroll_id"]
        #total_hits = response["hits"]["total"]

        #while len(results) < total_hits:
           # response = self.do_query_scroll(scroll_id)
           # results += [res["_source"]["tile_min_time_dt"] for res in response["hits"]["hits"]]
            
        #daysinrangeasc = sorted(
        #    [(datetime.strptime(a_date, ELASTICSEARCH_FORMAT) - datetime.utcfromtimestamp(0)).total_seconds() for a_date
        #     in results])
        
        daysinrangeasc = sorted([(res / 1000) for res in results])

        return daysinrangeasc

    def find_all_tiles_in_box_sorttimeasc(self, min_lat, max_lat, min_lon, max_lon, ds, start_time=0,
                                          end_time=-1, **kwargs):

        search = 'dataset_s:%s' % ds

        additionalparams = {
            'fq': [
                "geo:[%s,%s TO %s,%s]" % (min_lat, min_lon, max_lat, max_lon),
                "tile_count_i:[1 TO *]"
            ]
        }

        if 0 < start_time <= end_time:
            search_start_s = datetime.utcfromtimestamp(start_time).strftime(ELASTICSEARCH_FORMAT)
            search_end_s = datetime.utcfromtimestamp(end_time).strftime(ELASTICSEARCH_FORMAT)

            time_clause = "(" \
                          "tile_min_time_dt:[%s TO %s] " \
                          "OR tile_max_time_dt:[%s TO %s] " \
                          "OR (tile_min_time_dt:[* TO %s] AND tile_max_time_dt:[%s TO *])" \
                          ")" % (
                              search_start_s, search_end_s,
                              search_start_s, search_end_s,
                              search_start_s, search_end_s
                          )
            additionalparams['fq'].append(time_clause)

        self._merge_kwargs(additionalparams, **kwargs)

        return self.do_query_all(
            *(search, None, None, False, 'tile_min_time_dt asc, tile_max_time_dt asc'),
            **additionalparams)

    def find_all_tiles_in_polygon_sorttimeasc(self, bounding_polygon, ds, start_time=0, end_time=-1, **kwargs):

        #TODO : change hardcoded index name :
        search = "nexustiles"

        import re
        nums = re.findall(r'\d+(?:\.\d*)?', bounding_polygon.wkt.rpartition(',')[0])
        polygon_coordinates = zip(*[iter(nums)] * 2)

        max_lat = bounding_polygon.bounds[3]
        min_lon = bounding_polygon.bounds[0]
        min_lat = bounding_polygon.bounds[1]
        max_lon = bounding_polygon.bounds[2]

        self.logger.info("min_lat : {}".format(min_lat))
        self.logger.info("max_lat : {}".format(max_lat))
        self.logger.info("min_lon : {}".format(min_lon))
        self.logger.info("max_lon : {}".format(max_lon))

        additionalparams = {
            "query": {
                "bool": {
                    "filter": [
                        { 
                            "match": {
                                "dataset_s": ds
                            } 
                        },
                        {
                            "geo_shape": {
                                "geo": {
                                    "shape": {
                                        "type": "envelope",
                                        "coordinates": [[min_lon, max_lat],[max_lon, min_lat]]
                                    },
                                    "relation": "within"
                                }
                            }
                        }
                    ]
                }
            }
        }

        try:
            self.logger.info("find_all_tiles_in_polygon_sorttimeasc :: fields list : {}".format(kwargs["fl"]))
            if 'fl' in kwargs.keys():
                additionalparams["_source"] = kwargs["fl"].split(',')
        except KeyError:
            pass

        if 0 < start_time <= end_time:
            search_start_s = datetime.utcfromtimestamp(start_time).strftime(ELASTICSEARCH_FORMAT)
            search_end_s = datetime.utcfromtimestamp(end_time).strftime(ELASTICSEARCH_FORMAT)

            additionalparams["query"]["bool"]["should"] = [ 
                                                               { "range": {
                                                                   "tile_min_time_dt": {
                                                                       "lte": search_end_s,
                                                                       "gte": search_start_s
                                                                   }
                                                               } },
                                                               { "range": {
                                                                   "tile_max_time_dt": {
                                                                       "lte": search_end_s,
                                                                       "gte": search_start_s
                                                                   }
                                                               } },
                                                               { "bool": 
                                                                    { "must": [
                                                                       { "range": {
                                                                           "tile_min_time_dt": {
                                                                               "lte": search_start_s
                                                                           }
                                                                       } },
                                                                       { "range": {
                                                                           "tile_max_time_dt": {
                                                                               "gte": search_end_s
                                                                           }
                                                                       } }
                                                                   ] } 
                                                               }
                                                           ]


        self.logger.info("find_all_tiles_in_polygon_sorttimeasc :: additional params = {}".format(additionalparams))

        response = self.do_query_raw(*(search, "scroll", None, False, None), **additionalparams)

        self.logger.info("find_all_tiles_in_polygon_sorttimeasc  RESPONSE :")
        self.logger.info(response)

        results = response["hits"]["hits"]
        scroll_id = response["_scroll_id"]
        total_hits = response["hits"]["total"]

        try:
            while len(results) < total_hits:
                response = self.do_query_scroll(scroll_id)
                results.extend(response["hits"]["hits"])
        except:
            pass

        return [r["_source"] for r in results]

#        return self.do_query_all(
#            *(search, None, None, False, 'tile_min_time_dt:asc,tile_max_time_dt:asc'),
#            **additionalparams)

    def find_all_tiles_in_polygon(self, bounding_polygon, ds, start_time=0, end_time=-1, **kwargs):

        search = 'dataset_s:%s' % ds

        additionalparams = {
            'fq': [
                "{!field f=geo}Intersects(%s)" % bounding_polygon.wkt,
                "tile_count_i:[1 TO *]"
            ]
        }

        if 0 < start_time <= end_time:
            search_start_s = datetime.utcfromtimestamp(start_time).strftime(ELASTICSEARCH_FORMAT)
            search_end_s = datetime.utcfromtimestamp(end_time).strftime(ELASTICSEARCH_FORMAT)

            time_clause = "(" \
                          "tile_min_time_dt:[%s TO %s] " \
                          "OR tile_max_time_dt:[%s TO %s] " \
                          "OR (tile_min_time_dt:[* TO %s] AND tile_max_time_dt:[%s TO *])" \
                          ")" % (
                              search_start_s, search_end_s,
                              search_start_s, search_end_s,
                              search_start_s, search_end_s
                          )
            additionalparams['fq'].append(time_clause)

        self._merge_kwargs(additionalparams, **kwargs)

        return self.do_query_all(
            *(search, None, None, False, None),
            **additionalparams)

    def find_distinct_bounding_boxes_in_polygon(self, bounding_polygon, ds, start_time=0, end_time=-1, **kwargs):

        search = 'dataset_s:%s' % ds

        additionalparams = {
            'fq': [
                "{!field f=geo}Intersects(%s)" % bounding_polygon.wkt,
                "tile_count_i:[1 TO *]"
            ],
            'rows': 0,
            'facet': 'true',
            'facet.field': 'geo_s',
            'facet.limit': -1,
        }

        if 0 < start_time <= end_time:
            search_start_s = datetime.utcfromtimestamp(start_time).strftime(ELASTICSEARCH_FORMAT)
            search_end_s = datetime.utcfromtimestamp(end_time).strftime(ELASTICSEARCH_FORMAT)

            time_clause = "(" \
                          "tile_min_time_dt:[%s TO %s] " \
                          "OR tile_max_time_dt:[%s TO %s] " \
                          "OR (tile_min_time_dt:[* TO %s] AND tile_max_time_dt:[%s TO *])" \
                          ")" % (
                              search_start_s, search_end_s,
                              search_start_s, search_end_s,
                              search_start_s, search_end_s
                          )
            additionalparams['fq'].append(time_clause)

        self._merge_kwargs(additionalparams, **kwargs)

        response = self.do_query_raw(*(search, None, None, False, None), **additionalparams)

        distinct_bounds = [wkt.loads(key).bounds for key in response.facets["facet_fields"]["geo_s"][::2]]

        return distinct_bounds

    def find_tiles_by_exact_bounds(self, minx, miny, maxx, maxy, ds, start_time=0, end_time=-1, **kwargs):

        search = 'dataset_s:%s' % ds

        additionalparams = {
            'fq': [
                "tile_min_lon:\"%s\"" % minx,
                "tile_min_lat:\"%s\"" % miny,
                "tile_max_lon:\"%s\"" % maxx,
                "tile_max_lat:\"%s\"" % maxy,
                "tile_count_i:[1 TO *]"
            ]
        }

        if 0 < start_time <= end_time:
            search_start_s = datetime.utcfromtimestamp(start_time).strftime(ELASTICSEARCH_FORMAT)
            search_end_s = datetime.utcfromtimestamp(end_time).strftime(ELASTICSEARCH_FORMAT)

            time_clause = "(" \
                          "tile_min_time_dt:[%s TO %s] " \
                          "OR tile_max_time_dt:[%s TO %s] " \
                          "OR (tile_min_time_dt:[* TO %s] AND tile_max_time_dt:[%s TO *])" \
                          ")" % (
                              search_start_s, search_end_s,
                              search_start_s, search_end_s,
                              search_start_s, search_end_s
                          )
            additionalparams['fq'].append(time_clause)

        self._merge_kwargs(additionalparams, **kwargs)

        return self.do_query_all(
            *(search, None, None, False, None),
            **additionalparams)

    def find_all_tiles_in_box_at_time(self, min_lat, max_lat, min_lon, max_lon, ds, search_time, **kwargs):
        #search = 'dataset_s:%s' % ds
        #TODO : change hardcoded index name :
        search = "nexustiles"


        the_time = datetime.utcfromtimestamp(search_time).strftime(ELASTICSEARCH_FORMAT)


        additionalparams = {
            "size": 5000,
            "query": {
                "bool": {
                    "filter": [
                        {
                            "match": {
                                "dataset_s": ds
                            }
                        },
                        {
                            "geo_shape": {
                                "geo": {
                                    "shape": {
                                        "type": "envelope",
                                        "coordinates": [[min_lon, max_lat],[max_lon, min_lat]]
                                    },
                                    "relation": "within"
                                }
                            }
                        },
                        { "range": {
                            "tile_min_time_dt": {
                                "lte": the_time
                            }
                        } },
                        { "range": {
                            "tile_max_time_dt": {
                                "gte": the_time
                            }
                        } }
                    ]
                }
            }
        }

        self._merge_kwargs(additionalparams, **kwargs)

        #results = self.do_query_all(*(search, None, None, False, None), **additionalparams)

        response = self.do_query_raw(*(search, "scroll", None, False, None), **additionalparams)

        results = response["hits"]["hits"]
        scroll_id = response["_scroll_id"]
        total_hits = response["hits"]["total"]

        try:
            while len(results) < total_hits:
                self.logger.info("DAO : find_all_tiles_in_box_at_time : SCROLLING because {} < {}".format(len(results), total_hits))
                response = self.do_query_scroll(scroll_id)
                results.extend(response["hits"]["hits"])
                scroll_id = response["_scroll_id"]
        except KeyError:
            self.logger.info("DAO : find_all_tiles_in_box_at_time : End of scroll results.")
            pass

        self.logger.info("find_all_tiles_in_box_at_time :: result = {}".format(results))

        results_data = [res["_source"] for res in results]

        return results_data

    def find_all_tiles_in_box_at_time_and_depth(self, min_lat, max_lat, min_lon, max_lon, depth, ds, search_time, **kwargs):
        search = "nexustiles"


        the_time = datetime.utcfromtimestamp(search_time).strftime(ELASTICSEARCH_FORMAT)


        additionalparams = {
            "size": 5000,
            "query": {
                "bool": {
                    "filter": [
                        {
                            "match": {
                                "dataset_s": ds
                            }
                        },
			{
			    "match": {
                                "depth": depth
			    }
			},
                        {
                            "geo_shape": {
                                "geo": {
                                    "shape": {
                                        "type": "envelope",
                                        "coordinates": [[min_lon, max_lat],[max_lon, min_lat]]
                                    },
                                    "relation": "within"
                                }
                            }
                        },
                        { "range": {
                            "tile_min_time_dt": {
                                "lte": the_time
                            }
                        } },
                        { "range": {
                            "tile_max_time_dt": {
                                "gte": the_time
                            }
                        } }
                    ]
                }
            }
        }

        time_clause = "(" \
                      "tile_min_time_dt:[* TO %s] " \
                      "AND tile_max_time_dt:[%s TO *] " \
                      ")" % (
                          the_time, the_time
                      )

        response = self.do_query_raw(*(search, "scroll", None, False, None), **additionalparams)

        results = response["hits"]["hits"]
        scroll_id = response["_scroll_id"]
        total_hits = response["hits"]["total"]

        try:
            while len(results) < total_hits:
                self.logger.info("DAO : find_all_tiles_in_box_at_time : SCROLLING because {} < {}".format(len(results), total_hits))
                response = self.do_query_scroll(scroll_id)
                results.extend(response["hits"]["hits"])
                scroll_id = response["_scroll_id"]
        except KeyError:
            self.logger.info("DAO : find_all_tiles_in_box_at_time : End of scroll results.")
            pass

        self.logger.info("find_all_tiles_in_box_at_time :: result = {}".format(results))

        results_data = [res["_source"] for res in results]

        return results_data


    def find_all_tiles_in_polygon_at_time(self, bounding_polygon, ds, search_time, **kwargs):
        #search = 'dataset_s:%s' % ds
        search = 'nexustiles'

        the_time = datetime.utcfromtimestamp(search_time).strftime(ELASTICSEARCH_FORMAT)

        max_lat = bounding_polygon.bounds[3]
        min_lon = bounding_polygon.bounds[0]
        min_lat = bounding_polygon.bounds[1]
        max_lon = bounding_polygon.bounds[2]

        additionalparams = {
            "query": {
                "bool": {
                    "filter": [
                        {
                            "match": {
                                "dataset_s": ds
                            }
                        },
                        {
                            "geo_shape": {
                                "geo": {
                                    "shape": {
                                        "type": "envelope",
                                        "coordinates": [[min_lon, max_lat],[max_lon, min_lat]]
                                    },
                                    "relation": "within"
                                }
                            }
                        },
                        { "range": {
                            "tile_min_time_dt": {
                                "lte": the_time
                            }
                        } },
                        { "range": {
                            "tile_max_time_dt": {
                                "gte": the_time
                            }
                        } }
                    ]
                }
            }
        }


#        time_clause = "(" \
#                      "tile_min_time_dt:[* TO %s] " \
#                      "AND tile_max_time_dt:[%s TO *] " \
#                      ")" % (
#                          the_time, the_time
#                      )
#
#        additionalparams = {
#            'fq': [
#                "{!field f=geo}Intersects(%s)" % bounding_polygon.wkt,
#                "tile_count_i:[1 TO *]",
#                time_clause
#            ]
#        }
#
#        self._merge_kwargs(additionalparams, **kwargs)

        response = self.do_query_raw(*(search, "scroll", None, False, None), **additionalparams)

        results = response["hits"]["hits"]
        scroll_id = response["_scroll_id"]
        total_hits = response["hits"]["total"]

        try:
            while len(results) < total_hits:
                self.logger.info("DAO : find_all_tiles_in_polygon_at_time : SCROLLING because {} < {}".format(len(results), total_hits))
                response = self.do_query_scroll(scroll_id)
                results.extend(response["hits"]["hits"])
                scroll_id = response["_scroll_id"]
        except KeyError:
            self.logger.info("DAO : find_all_tiles_in_polygon_at_time : End of scroll results.")
            pass

        results_data = [result["_source"] for result in results]
        return results_data
        #return self.do_query_all(*(search, None, None, False, None), **additionalparams)


    def find_all_tiles_within_box_at_time(self, min_lat, max_lat, min_lon, max_lon, ds, time, **kwargs):
        search = 'dataset_s:%s' % ds

        the_time = datetime.utcfromtimestamp(time).strftime(ELASTICSEARCH_FORMAT)
        time_clause = "(" \
                      "tile_min_time_dt:[* TO %s] " \
                      "AND tile_max_time_dt:[%s TO *] " \
                      ")" % (
                          the_time, the_time
                      )

        additionalparams = {
            'fq': [
                "geo:\"Within(ENVELOPE(%s,%s,%s,%s))\"" % (min_lon, max_lon, max_lat, min_lat),
                "tile_count_i:[1 TO *]",
                time_clause
            ]
        }

        self._merge_kwargs(additionalparams, **kwargs)

        return self.do_query_all(*(search, "product(tile_avg_val_d, tile_count_i),*", None, False, None),
                                 **additionalparams)

    def find_all_boundary_tiles_at_time(self, min_lat, max_lat, min_lon, max_lon, ds, time, **kwargs):
        search = 'dataset_s:%s' % ds

        the_time = datetime.utcfromtimestamp(time).strftime(ELASTICSEARCH_FORMAT)
        time_clause = "(" \
                      "tile_min_time_dt:[* TO %s] " \
                      "AND tile_max_time_dt:[%s TO *] " \
                      ")" % (
                          the_time, the_time
                      )

        additionalparams = {
            'fq': [
                "geo:\"Intersects(MultiLineString((%s %s, %s %s),(%s %s, %s %s),(%s %s, %s %s),(%s %s, %s %s)))\"" % (
                    min_lon, max_lat, max_lon, max_lat, min_lon, max_lat, min_lon, min_lat, max_lon, max_lat, max_lon,
                    min_lat, min_lon, min_lat, max_lon, min_lat),
                "-geo:\"Within(ENVELOPE(%s,%s,%s,%s))\"" % (min_lon, max_lon, max_lat, min_lat),
                "tile_count_i:[1 TO *]",
                time_clause
            ]
        }

        self._merge_kwargs(additionalparams, **kwargs)

        return self.do_query_all(*(search, None, None, False, None), **additionalparams)

    def find_all_tiles_by_metadata(self, metadata, ds, start_time=0, end_time=-1, **kwargs):
        """
        Get a list of tile metadata that matches the specified metadata, start_time, end_time.
        :param metadata: List of metadata values to search for tiles e.g ["river_id_i:1", "granule_s:granule_name"]
        :param ds: The dataset name to search
        :param start_time: The start time to search for tiles
        :param end_time: The end time to search for tiles
        :return: A list of tile metadata
        """
        search = 'dataset_s:%s' % ds

        additionalparams = {
            'fq': metadata
        }

        if 0 < start_time <= end_time:
            additionalparams['fq'].append(self.get_formatted_time_clause(start_time, end_time))

        self._merge_kwargs(additionalparams, **kwargs)

        return self.do_query_all(
            *(search, None, None, False, None),
            **additionalparams)

    def get_formatted_time_clause(self, start_time, end_time):
        search_start_s = datetime.utcfromtimestamp(start_time).strftime(ELASTICSEARCH_FORMAT)
        search_end_s = datetime.utcfromtimestamp(end_time).strftime(ELASTICSEARCH_FORMAT)

        time_clause = "(" \
                      "tile_min_time_dt:[%s TO %s] " \
                      "OR tile_max_time_dt:[%s TO %s] " \
                      "OR (tile_min_time_dt:[* TO %s] AND tile_max_time_dt:[%s TO *])" \
                      ")" % (
                          search_start_s, search_end_s,
                          search_start_s, search_end_s,
                          search_start_s, search_end_s
                          )
        return time_clause

    def get_tile_count(self, ds, bounding_polygon=None, start_time=0, end_time=-1, metadata=None, **kwargs):
        """
        Return number of tiles that match search criteria.
        :param ds: The dataset name to search
        :param bounding_polygon: The polygon to search for tiles
        :param start_time: The start time to search for tiles
        :param end_time: The end time to search for tiles
        :param metadata: List of metadata values to search for tiles e.g ["river_id_i:1", "granule_s:granule_name"]
        :return: number of tiles that match search criteria
        """
        search = 'dataset_s:%s' % ds

        additionalparams = {
            'fq': [
                "tile_count_i:[1 TO *]"
            ],
            'rows': 0
        }

        if bounding_polygon:
            min_lon, min_lat, max_lon, max_lat = bounding_polygon.bounds
            additionalparams['fq'].append("geo:[%s,%s TO %s,%s]" % (min_lat, min_lon, max_lat, max_lon))

        if 0 < start_time <= end_time:
            additionalparams['fq'].append(self.get_formatted_time_clause(start_time, end_time))

        if metadata:
            additionalparams['fq'].extend(metadata)

        self._merge_kwargs(additionalparams, **kwargs)

        results, start, found = self.do_query(*(search, None, None, True, None), **additionalparams)

        return found

    def do_query(self, *args, **params):

        response = self.do_query_raw(*args, **params)
        #self.logger.info(response)

        return response.docs, response.raw_response['response']['start'], response.hits

    def do_query_raw(self, *args, **params):

        #self.logger.info("QUERY RAW ARGS :")
        #self.logger.info(args)

        if 'sort' not in params.keys() and args[4]:
            #params['sort'] = args[4]
            sort_fields = args[4].split(",")
            params["sort"] = []

            for field in sort_fields:
                field_order = field.split(':')
                params["sort"].append({field_order[0]: field_order[1]})


        #self.logger.info("params : {}".format(params))
        with ELASTICSEARCH_CON_LOCK:
            if args[1] is not None and args[1] == "scroll":
                response = self.elasticsearchcon.search(index=args[0], body=params, scroll="1m")
            else:
                response = self.elasticsearchcon.search(index=args[0], body=params)

        return response


    def do_query_scroll(self, scroll_id):
        with ELASTICSEARCH_CON_LOCK:
            response = self.elasticsearchcon.scroll(scroll_id=scroll_id)
        return response


    def do_query_all(self, *args, **params):

        results = []

        self.logger.info("do_query_all :: params = {}".format(params))
        search = None
        response = self.do_query_raw(*(search, "scroll", None, False, None), **params)

        results.extend(response)

        scroll_id = response["_scroll_id"]
        total_hits = response["hits"]["total"]

        try:
            while len(results) < total_hits:
                self.logger.info("do_query_all :: results = {}".format(results))
                self.logger.info("DAO : SCROLLING because {} < {}".format(len(results), total_hits))
                response = self.do_query_scroll(scroll_id)
                self.logger.info("do_query_all :: response = {}".format(response))
                results.extend(response)
                scroll_id = response["_scroll_id"]
        except KeyError:
            self.logger.info("DAO : End of scroll results.")
            pass

        return results

    def convert_iso_to_datetime(self, date):
        return datetime.strptime(date, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)

    def convert_iso_to_timestamp(self, date):
        return (self.convert_iso_to_datetime(date) - EPOCH).total_seconds()

    # def ping(self):
    #     solrAdminPing = 'http://%s/solr/%s/admin/ping' % (self.solrUrl, self.solrCore)
    #     try:
    #         r = requests.get(solrAdminPing, params={'wt': 'json'})
    #         results = json.loads(r.text)
    #         return results
    #     except:
    #         return None

    @staticmethod
    def _merge_kwargs(additionalparams, **kwargs):
        # Only Solr-specific kwargs are parsed
        # And the special 'limit'
        try:
            additionalparams['limit'] = kwargs['limit']
        except KeyError:
            pass

        try:
            additionalparams['_route_'] = kwargs['_route_']
        except KeyError:
            pass

        try:
            additionalparams['size'] = kwargs['size']
        except KeyError:
            pass

        try:
            additionalparams['start'] = kwargs['start']
        except KeyError:
            pass

        #try:
        #    kwfq = kwargs['fq'] if isinstance(kwargs['fq'], list) else list(kwargs['fq'])
        #except KeyError:
        #    kwfq = []

        #try:
        #    additionalparams['fq'].extend(kwfq)
        #except KeyError:
        #    additionalparams['fq'] = kwfq

        #try:
        #    kwfl = kwargs['fl'] if isinstance(kwargs['fl'], list) else [kwargs['fl']]
        #except KeyError:
        #    kwfl = []

        #try:
        #    additionalparams['fl'].extend(kwfl)
        #except KeyError:
        #    additionalparams['fl'] = kwfl

        try:
            s = kwargs['sort'] if isinstance(kwargs['sort'], list) else [kwargs['sort']]
        except KeyError:
            s = None

        try:
            additionalparams['sort'].extend(s)
        except KeyError:
            if s is not None:
                additionalparams['sort'] = s
