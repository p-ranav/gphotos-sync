#!/usr/bin/env python3
# coding: utf8
import os.path

from gphotos import Utils
from gphotos.GooglePhotosMedia import GooglePhotosMedia
from gphotos.BaseMedia import MediaType
from gphotos.LocalData import LocalData
from gphotos.DatabaseMedia import DatabaseMedia
from gphotos.BadIds import BadIds

from itertools import zip_longest
import logging
import shutil
import tempfile
import concurrent.futures as futures

import requests
import requests.exceptions as err
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)


class GooglePhotosSync(object):
    PAGE_SIZE = 100
    MAX_THREADS = 20
    BATCH_SIZE = 40

    def __init__(self, api, root_folder, db):
        """
        :param (RestClient) api
        :param (str) root_folder:
        :param (LocalData) db:
        """
        self._db = db
        self._root_folder = root_folder
        self._api = api
        self._media_folder = 'photos'
        self.download_pool = futures.ThreadPoolExecutor(
            max_workers=self.MAX_THREADS)
        self.pool_future_to_media = {}
        self.bad_ids = BadIds(root_folder)

        self.files_downloaded = 0
        self.files_download_started = 0
        self.files_download_skipped = 0
        self.files_download_failed = 0
        self.files_indexed = 0
        self.files_index_skipped = 0

        self._latest_download = self._db.get_scan_date() or Utils.minimum_date()
        # properties to be set after init
        # thus in theory one instance could so multiple indexes
        self._startDate = None
        self._endDate = None
        self.includeVideo = True
        self._rescan = False
        self._retry_download = False
        self._video_timeout = 2000
        self._image_timeout = 60

        self._session = requests.Session()
        retries = Retry(total=5,
                        backoff_factor=0.1,
                        status_forcelist=[500, 502, 503, 504])

        self._session.mount(
            'https://', HTTPAdapter(max_retries=retries,
                                    pool_maxsize=self.MAX_THREADS))

    @property
    def video_timeout(self):
        return self._video_timeout

    @video_timeout.setter
    def video_timeout(self, val):
        self._video_timeout = val

    @property
    def image_timeout(self):
        return self._image_timeout

    @image_timeout.setter
    def image_timeout(self, val):
        self._image_timeout = val

    @property
    def start_date(self):
        return self._startDate

    @start_date.setter
    def start_date(self, val):
        if val:
            self._startDate = Utils.string_to_date(val)

    @property
    def end_date(self):
        return self._endDate

    @end_date.setter
    def end_date(self, val):
        if val:
            self._endDate = Utils.string_to_date(val)

    @property
    def latest_download(self):
        return self._latest_download

    @property
    def rescan(self):
        return self._rescan

    @rescan.setter
    def rescan(self, val):
        self._rescan = val

    @property
    def retry_download(self):
        return self._retry_download

    @retry_download.setter
    def retry_download(self, val):
        self._retry_download = val

    def check_for_removed(self):
        # note for partial scans using date filters this is still OK because
        # for a file to exist it must have been indexed in a previous scan
        log.warning('Finding and removing deleted media ...')
        start_folder = os.path.join(self._root_folder, self._media_folder)
        for (dir_name, _, file_names) in os.walk(start_folder):
            for file_name in file_names:
                local_path = os.path.relpath(dir_name, self._root_folder)
                if file_name.startswith('.') or file_name.startswith('gphotos'):
                    continue
                file_row = self._db.get_file_by_path(local_path, file_name)
                if not file_row:
                    name = os.path.join(dir_name, file_name)
                    os.remove(name)
                    log.warning("%s deleted", name)

    def write_media_index(self, media, update=True):
        media.save_to_db(self._db, update)
        if media.create_date > self._latest_download:
            self._latest_download = media.create_date

    def search_media(self, page_token=None, start_date=None, end_date=None,
                     do_video=False):
        class Y:
            def __init__(self, y, m, d):
                self.year = y
                self.month = m
                self.day = d

            def to_dict(self):
                return {"year": self.year, "month": self.month, "day": self.day}

        if not page_token:
            log.info('searching for media start=%s, end=%s, videos=%s',
                     start_date, end_date, do_video)
        if not start_date and not end_date and do_video:
            # no search criteria so do a list of the entire library
            return self._api.mediaItems.list.execute(
                pageToken=page_token, pageSize=self.PAGE_SIZE).json()
        else:
            start = Y(1900, 1, 1)
            end = Y(3000, 1, 1)
            type_list = ["ALL_MEDIA"]

            if start_date:
                start = Y(start_date.year, start_date.month, start_date.day)
            if end_date:
                end = Y(end_date.year, end_date.month, end_date.day)
            if not do_video:
                type_list = ["PHOTO"]

            body = {
                'pageToken': page_token,
                'pageSize': self.PAGE_SIZE,
                'filters': {
                    'dateFilter': {
                        'ranges':
                            [
                                {'startDate': start.to_dict(),
                                 'endDate': end.to_dict()
                                 }
                            ]
                    },
                    'mediaTypeFilter': {'mediaTypes': type_list},
                }
            }
            return self._api.mediaItems.search.execute(body).json()

    def index_photos_media(self):
        log.warning('Indexing Google Photos Files ...')

        if self._rescan:
            start_date = None
        else:
            start_date = self.start_date or self._db.get_scan_date()

        items_json = self.search_media(start_date=start_date,
                                       end_date=self.end_date,
                                       do_video=self.includeVideo)
        while items_json:
            media_json = items_json.get('mediaItems')
            # cope with empty response
            if not media_json:
                break
            for media_item_json in media_json:
                media_item = GooglePhotosMedia(media_item_json)
                media_item.set_path_by_date(self._media_folder)
                row = media_item.is_indexed(self._db)
                if not row:
                    self.files_indexed += 1
                    log.info("Indexed %d %s", self.files_indexed,
                             media_item.relative_path)
                    self.write_media_index(media_item, False)
                    if self.files_indexed % 2000 == 0:
                        self._db.store()
                elif media_item.modify_date > row.ModifyDate:
                    self.files_indexed += 1
                    # todo at present there is no modify date in the API
                    #  so updates cannot be monitored
                    log.info("Updated Index %d %s", self.files_indexed,
                             media_item.relative_path)
                    self.write_media_index(media_item, True)
                else:
                    self.files_index_skipped += 1
                    log.debug("Skipped Index (already indexed) %d %s",
                              self.files_index_skipped,
                              media_item.relative_path)
            next_page = items_json.get('nextPageToken')
            if next_page:
                items_json = self.search_media(page_token=next_page,
                                               start_date=start_date,
                                               end_date=self.end_date,
                                               do_video=self.includeVideo)
            else:
                break

        # scan (in reverse date order) completed so the next incremental scan
        # can start from the most recent file in this scan
        if not self.start_date:
            self._db.set_scan_date(last_date=self._latest_download)

    def do_download_file(self, base_url, media_item):
        # this function runs in a process pool and does the actual downloads
        local_folder = os.path.join(self._root_folder,
                                    media_item.relative_folder)
        local_full_path = os.path.join(local_folder, media_item.filename)
        if media_item.is_video():
            download_url = '{}=dv'.format(base_url)
            timeout = self._video_timeout
        else:
            download_url = '{}=d'.format(base_url)
            timeout = self._image_timeout
        temp_file = tempfile.NamedTemporaryFile(dir=local_folder, delete=False)

        try:
            response = self._session.get(download_url, stream=True,
                                         timeout=timeout)
            shutil.copyfileobj(response.raw, temp_file)
            temp_file.close()
            response.close()
            os.rename(temp_file.name, local_full_path)
            os.utime(local_full_path,
                     (Utils.to_timestamp(media_item.modify_date),
                      Utils.to_timestamp(media_item.create_date)))
        except KeyboardInterrupt:
            log.debug("User cancelled download thread")
            raise
        except BaseException:
            os.remove(temp_file.name)
            raise

    def do_download_complete(self, futures_list):
        for future in futures_list:
            media_item = self.pool_future_to_media.get(future)
            e = future.exception()
            if e:
                self.files_download_failed += 1
                log.error('FAILURE %d downloading %s',
                          self.files_download_failed, media_item.relative_path)
                log.debug('FAILURE %d downloading %s',
                          self.files_download_failed, media_item.relative_path,
                          exc_info=e)
                if isinstance(e, requests.HTTPError):
                    self.bad_ids.add_id(
                        media_item.relative_path, media_item.id,
                        media_item.url)
                if e == KeyboardInterrupt:
                    raise e
            else:
                self._db.put_downloaded(media_item.id)
                self.files_downloaded += 1
                log.debug('COMPLETED %d downloading %s',
                          self.files_downloaded, media_item.relative_path)
            del self.pool_future_to_media[future]

    def download_file(self, media_item, media_json):
        """ farms media downloads off to the thread pool"""
        base_url = media_json['baseUrl']

        # we dont want a massive queue so wait until at least one thread is free
        while len(self.pool_future_to_media) >= self.MAX_THREADS:
            # check which futures are done, complete the main thread work
            # and remove them from the dictionary
            done_list = []
            for future in self.pool_future_to_media.keys():
                if future.done():
                    done_list.append(future)

            self.do_download_complete(done_list)

        # start a new background download
        self.files_download_started += 1
        log.info('downloading %d %s', self.files_download_started,
                 media_item.relative_path)
        future = self.download_pool.submit(self.do_download_file,
                                           base_url, media_item)
        self.pool_future_to_media[future] = media_item

    def download_photo_media(self):
        """
        here we batch up our requests to get baseurl for downloading media.
        This avoids the overhead of one REST call per file. A REST call
        takes longer than downloading an image
        """

        def grouper(iterable):
            """Collect data into chunks size BATCH_SIZE"""
            return zip_longest(*[iter(iterable)] * self.BATCH_SIZE,
                               fillvalue=None)

        log.warning('Downloading Photos ...')
        try:
            for media_items_block in grouper(
                    # todo get rid of mediaType
                    DatabaseMedia.get_media_by_search(
                        self._db,
                        media_type=MediaType.PHOTOS,
                        start_date=self.start_date,
                        end_date=self.end_date,
                        skip_downloaded=not self._retry_download)):
                batch = {}

                items = (mi for mi in media_items_block if mi)
                for media_item in items:
                    local_folder = os.path.join(
                        self._root_folder, media_item.relative_folder)
                    local_full_path = os.path.join(
                        local_folder, media_item.filename)

                    if os.path.exists(local_full_path):
                        self.files_download_skipped += 1
                        log.debug('SKIPPED download (file exists) %d %s',
                                  self.files_download_skipped,
                                  media_item.relative_path)
                        self._db.put_downloaded(media_item.id)

                    elif self.bad_ids.check_id_ok(media_item.id):
                        batch[media_item.id] = media_item
                        if not os.path.isdir(local_folder):
                            os.makedirs(local_folder)

                if len(batch) > 0:
                    self.download_batch(batch)
        finally:
            # allow any remaining background downloads to complete
            futures_left = list(self.pool_future_to_media.keys())
            self.do_download_complete(futures_left)
            log.warning(
                'Downloaded %d Items, Failed %d, Already Downloaded %d',
                self.files_downloaded, self.files_download_failed,
                self.files_download_skipped)
            self.bad_ids.store_ids()
            self.bad_ids.report()

    def download_batch(self, batch):
        try:
            response = self._api.mediaItems.batchGet.execute(
                mediaItemIds=batch.keys())
            r_json = response.json()
            if r_json.get('pageToken'):
                log.error("Ops - Batch size too big, some items dropped!")

            for media_item_json_status in r_json["mediaItemResults"]:
                # todo look at media_item_json_status["status"] for errors
                media_item_json = media_item_json_status.get("mediaItem")
                if not media_item_json:
                    log.warning('Null response in mediaItems.batchGet %s',
                                batch.keys())
                else:
                    media_item = batch.get(media_item_json["id"])
                    media_item.set_path_by_date(self._media_folder)
                    self.download_file(media_item, media_item_json)

        except KeyboardInterrupt:
            log.warning('Cancelling download threads ...')
            for f in self.pool_future_to_media:
                f.cancel()
            log.warning('Cancelled download threads')
            raise
        except (err.HTTPError, err.RetryError):
            self.find_bad_items(batch)

    def find_bad_items(self, batch):
        """
        a batch get failed. Now do all of its contents as individual
        gets so we can work out which ID(s) cause the failure
        """
        for item_id, media_item in batch.items():
            media_item.set_path_by_date(self._media_folder)
            try:
                response = self._api.mediaItems.get.execute(item_id)
                media_item_json = response.json()
                self.download_file(media_item, media_item_json)
            except (err.HTTPError, err.RetryError):
                self.bad_ids.add_id(
                    media_item.relative_path, media_item.id,
                    media_item.url)
                self.files_download_failed += 1
                log.error('FAILURE %d in get of %s BAD ID',
                          self.files_download_failed, media_item.relative_path)
                log.debug('FAILURE %d in get of %s BAD ID',
                          self.files_download_failed, media_item.relative_path,
                          exc_info=True)