import importlib.util, pathlib, sys, unittest, urllib.error
from unittest import mock
SCRIPT = pathlib.Path(__file__).parents[1]/"scripts"/"sync_youtube_airtable.py"
SPEC = importlib.util.spec_from_file_location("sync_youtube_airtable", SCRIPT)
sync = importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name]=sync; SPEC.loader.exec_module(sync)
def video(vid="tRNVu1W-IRs"):
    return {"id":vid,"snippet":{"channelId":sync.CHANNEL_ID,"title":"오늘의 영양","description":"설명 #건강",
      "publishedAt":"2026-10-13T01:02:03Z","tags":["영양"],"thumbnails":{"high":{"url":"https://img/high.jpg"}}},
      "status":{"privacyStatus":"public"}}
class FakeYT:
    items={"tRNVu1W-IRs":video()}; ids=["tRNVu1W-IRs","tRNVu1W-IRs"]
    def __init__(self,token): pass
    def uploads_playlist(self): return "UUplaylist"
    def upload_ids(self,playlist,full=False): self.__class__.full=full; return sync.dedupe(self.ids)
    def metadata(self,ids): return {x:self.items[x] for x in ids if x in self.items}
class FakeAT:
    old={}; creates=[]; updates=[]
    def __init__(self): pass
    def index(self): return dict(self.old)
    def write_batches(self,creates,updates): self.__class__.creates,self.__class__.updates=creates,updates
class SyncTests(unittest.TestCase):
    def setUp(self):
        FakeAT.old,FakeAT.creates,FakeAT.updates={},[],[]
        FakeYT.items,FakeYT.ids={"tRNVu1W-IRs":video()},["tRNVu1W-IRs","tRNVu1W-IRs"]
        self.p=[mock.patch.object(sync,"oauth_token",return_value="token"),mock.patch.object(sync,"YouTube",FakeYT),mock.patch.object(sync,"Airtable",FakeAT)]
        [x.start() for x in self.p]
    def tearDown(self): [x.stop() for x in reversed(self.p)]
    def test_new_video_once_and_actual_publish_date(self):
        self.assertEqual(sync.sync(),(1,0)); self.assertEqual(len(FakeAT.creates),1)
        self.assertEqual(FakeAT.creates[0]["업로드일"],"2026-10-13T01:02:03Z"); self.assertNotIn("transcript",FakeAT.creates[0])
    def test_existing_data_preserved_and_no_duplicate(self):
        FakeAT.old={"tRNVu1W-IRs":{"id":"rec","fields":{"title":"사용자 제목","url":"https://youtu.be/tRNVu1W-IRs",
          "썸네일":"custom","설명":"custom","업로드일":"2020-01-01","privacyStatus":"private","hashtags":"custom","transcript":"기존"}}}
        self.assertEqual(sync.sync(),(0,0)); self.assertFalse(FakeAT.creates); self.assertFalse(FakeAT.updates)
    def test_only_empty_fields_filled_transcript_untouched(self):
        FakeAT.old={"tRNVu1W-IRs":{"id":"rec","fields":{"url":"https://youtu.be/tRNVu1W-IRs","title":"보존","transcript":""}}}
        self.assertEqual(sync.sync(),(0,1)); fields=FakeAT.updates[0][1]
        self.assertNotIn("title",fields); self.assertNotIn("transcript",fields); self.assertIn("업로드일",fields)
    def test_full_backfill_separate(self):
        sync.sync(full=True); self.assertTrue(FakeYT.full); sync.sync(); self.assertFalse(FakeYT.full)
    def test_required_video_validation(self):
        with self.assertRaises(sync.SyncError): sync.sync(required_ids=["abcdefghijk"])
class ApiTests(unittest.TestCase):
    def test_recent_mode_stops_after_first_page_without_error(self):
        yt=sync.YouTube("token"); yt.api=mock.Mock(return_value={"items":[{"contentDetails":{"videoId":"tRNVu1W-IRs"}}],"nextPageToken":"more"})
        self.assertEqual(yt.upload_ids("UUplaylist",full=False),["tRNVu1W-IRs"]); self.assertEqual(yt.api.call_count,1)
    def test_oauth_failure_is_fatal_and_secret_safe(self):
        env={"YOUTUBE_CLIENT_ID":"id","YOUTUBE_CLIENT_SECRET":"TOP_SECRET","YOUTUBE_REFRESH_TOKEN":"refresh"}
        error=urllib.error.HTTPError("token",400,"bad",{},None)
        with mock.patch.dict("os.environ",env,clear=True),mock.patch("urllib.request.urlopen",side_effect=error):
            with self.assertRaises(sync.SyncError) as raised: sync.oauth_token()
        self.assertNotIn("TOP_SECRET",str(raised.exception))
    def test_airtable_error_retry_is_bounded(self):
        error=urllib.error.HTTPError("airtable",500,"bad",{},None)
        with mock.patch("urllib.request.urlopen",side_effect=error) as called,mock.patch("time.sleep"):
            with self.assertRaises(sync.SyncError): sync.request_json("https://api.airtable.com",service="Airtable API",retries=2)
        self.assertEqual(called.call_count,2)
    def test_metadata_batches_fifty(self):
        yt=sync.YouTube("token"); yt.api=mock.Mock(return_value={"items":[]}); ids=[f"vid{i:08d}" for i in range(51)]
        yt.metadata(ids); self.assertEqual(yt.api.call_count,2)
    def test_no_ytdlp_or_transcript_write(self):
        source=SCRIPT.read_text(encoding="utf-8"); self.assertNotIn("yt_dlp",source); self.assertNotIn('["transcript"]',source)
    def test_workflow_safety_controls(self):
        workflow=(SCRIPT.parents[1]/".github/workflows/youtube-airtable-sync.yml").read_text(encoding="utf-8")
        self.assertIn("cron: '17 * * * *'",workflow); self.assertIn("timeout-minutes: 10",workflow)
        self.assertIn("concurrency:",workflow); self.assertIn("full_backfill:",workflow)
if __name__=="__main__": unittest.main()
