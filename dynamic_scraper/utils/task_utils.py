import datetime, json
import urllib, urllib2, httplib
from billiard.process import Process
from scrapy import log
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings
settings = get_project_settings()
from django.conf import settings as django_settings
from dynamic_scraper.models import Scraper
from django.core.cache import cache
import logging
from celery import shared_task

logger = logging.getLogger(__name__)

class TaskUtils():

    conf = {
        "MAX_SPIDER_RUNS_PER_TASK": 10,
        "MAX_CHECKER_RUNS_PER_TASK": 25,
    }

    def _run_spider(self, **kwargs):
        param_dict = {
            'project': 'default',
            'spider': kwargs['spider'],
            'id': kwargs['id'],
            'run_type': kwargs['run_type'],
            'do_action': kwargs['do_action']
        }
        params = urllib.urlencode(param_dict)
        headers = {"Content-type": "application/x-www-form-urlencoded", "Accept": "text/plain"}
        conn = httplib.HTTPConnection("localhost:6800")
        conn.request("POST", "/schedule.json", params, headers)
        conn.getresponse()


    def _pending_jobs(self, spider):
        # Ommit scheduling new jobs if there are still pending jobs for same spider
        resp = urllib2.urlopen('http://localhost:6800/listjobs.json?project=default')
        data = json.load(resp)
        if 'pending' in data:
            for item in data['pending']:
                if item['spider'] == spider:
                    return True
        return False


    def run_spiders(self, ref_obj_class, scraper_field_name, runtime_field_name, spider_name):

        kwargs = {
            '%s__status' % scraper_field_name: 'A',
            '%s__next_action_time__lt' % runtime_field_name: datetime.datetime.now(),
        }

        max = settings.get('DSCRAPER_MAX_SPIDER_RUNS_PER_TASK', self.conf['MAX_SPIDER_RUNS_PER_TASK'])
        ref_obj_list = ref_obj_class.objects.filter(**kwargs).order_by('%s__next_action_time' % runtime_field_name)[:max]
        if not self._pending_jobs(spider_name):
            for ref_object in ref_obj_list:
                self._run_spider(id=ref_object.pk, spider=spider_name, run_type='TASK', do_action='yes')


    def run_checkers(self, ref_obj_class, scraper_field_path, runtime_field_name, checker_name):

        kwargs = {
            '%s__status' % scraper_field_path: 'A',
            '%s__next_action_time__lt' % runtime_field_name: datetime.datetime.now(),
        }
        kwargs2 = {
            '%s__checker_type' % scraper_field_path: 'N',
        }

        max = settings.get('DSCRAPER_MAX_CHECKER_RUNS_PER_TASK', self.conf['MAX_CHECKER_RUNS_PER_TASK'])
        ref_obj_list = ref_obj_class.objects.filter(**kwargs).exclude(**kwargs2).order_by('%s__next_action_time' % runtime_field_name)[:max]
        if not self._pending_jobs(checker_name):
            for ref_object in ref_obj_list:
                self._run_spider(id=ref_object.pk, spider=checker_name, run_type='TASK', do_action='yes')


    def run_checker_tests(self):

        scraper_list = Scraper.objects.filter(checker_x_path__isnull=False, checker_x_path_result__isnull=False, checker_x_path_ref_url__isnull=False)

        for scraper in scraper_list:
            self._run_spider(id=scraper.id, spider='checker_test', run_type='TASK', do_action='yes')


class ProcessBasedUtils(TaskUtils):

    # settings are defined in the manage.py file
    # set the SCRAPY_SETTINGS_MODULE path in manage.py
    # Ex:
    # os.environ.setdefault("DJANGO_SETTINGS_MODULE", "scrapy_test.settings.dev")
    # os.environ.setdefault("SCRAPY_SETTINGS_MODULE", "scrapy_test.apps.web_scraper.settings") <-- IMPORTANT

    # how to get settings: http://stackoverflow.com/questions/15564844/locally-run-all-of-the-spiders-in-scrapy

    def _run_spider(self, **kwargs):
      _run_spider_task.delay(**kwargs)

    def _pending_jobs(self, spider):
        # don't worry about scheduling new jobs if there are still pending jobs for same spider
        return False

def _run_crawl_process(**kwargs):
  #log.start must be explicitly called
  log.start(loglevel=getattr(django_settings, 'SCRAPY_LOG_LEVEL', 'INFO'))

  # region How to run a crawler in-process
  # examples on how to get this stuff:
  # http://stackoverflow.com/questions/14777910/scrapy-crawl-from-script-always-blocks-script-execution-after-scraping?lq=1
  # http://stackoverflow.com/questions/13437402/how-to-run-scrapy-from-within-a-python-script
  # http://stackoverflow.com/questions/7993680/running-scrapy-tasks-in-python
  # http://stackoverflow.com/questions/15564844/locally-run-all-of-the-spiders-in-scrapy
  # https://groups.google.com/forum/#!topic/scrapy-users/d4axj6nPVDw
  # endregion

  crawler = CrawlerProcess(settings)
  crawler.install()
  crawler.configure()
  spider = crawler.spiders.create(kwargs['spider'], **kwargs)
  crawler.crawl(spider)


  log.msg('Spider started...')
  crawler.start()
  log.msg('Spider stopped.')
  crawler.stop()

@shared_task
def _run_spider_task(**kwargs):
  # the reason we're checking here and not `pending_jobs` is because this gives more useful info to make the
  # decision

  cache_key = "{0}-lock-{1}".format(kwargs['spider'], kwargs['id'])

  if cache.add(cache_key, True):

    logger.debug("Cache added: {0}".format(cache_key))

    try:
      param_dict = {
        'project': 'default',
        'spider': kwargs['spider'],
        'id': kwargs['id'],
        'run_type': kwargs['run_type'],
        'do_action': kwargs['do_action']
      }
      p = Process(target=_run_crawl_process, kwargs=param_dict)
      p.start()
      p.join()

    finally:
      logger.debug("Cache removed: {0}".format(cache_key))
      cache.delete(cache_key)

  else:
    logger.info("Spider not started. {0} is alreadying running".format(cache_key))
