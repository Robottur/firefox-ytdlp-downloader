"""Retention exercises real SQLite and queue transitions without site traffic."""
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from ytdlp_host import jobs


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'jobs.db'
        self.store = jobs.JobStore(self.db)
        self.addCleanup(lambda: self.store.close())
        self.manager = self.manager_for(self.store)

    def manager_for(self, store):
        return jobs.JobManager(store, lambda message: None, True,
                               default_dir=self.temp.name, log=lambda message: None)

    def submit(self, **spec):
        return self.manager.submit({'url': 'https://example.com/video', **spec})

    def test_terminal_states_leave_memory_retry_but_no_durable_history(self):
        for status in ('complete', 'error', 'cancelled'):
            with self.subTest(status=status):
                job = self.submit()
                self.manager._finish(job, status, error='private error')
                self.assertIsNone(self.store.get(job.id))
                self.assertIn(job.id, [j.id for j in self.manager.list_jobs()])
                retry = self.manager.retry(job.id)
                self.assertEqual(retry.url, job.url)
                self.assertNotEqual(retry.id, job.id)
        self.assertTrue(all(j.status == 'queued' for j in self.manager_for(self.store).list_jobs()))

    def test_interrupted_jobs_survive_restarts_and_can_be_dismissed(self):
        job = self.submit()
        self.manager._set_status(job, 'downloading')
        restarted = self.manager_for(self.store)
        restarted.recover()
        self.assertEqual(self.store.get(job.id).status, 'interrupted')
        again = self.manager_for(self.store)
        again.recover()
        self.assertEqual(again.list_jobs()[0].status, 'interrupted')
        self.assertEqual(again.retry(job.id).url, job.url)
        self.assertTrue(again.dismiss(job.id))
        self.assertIsNone(self.store.get(job.id))

    def test_shutdown_preserves_recoverable_record(self):
        job = self.submit()
        self.manager.shutdown()
        self.manager._finish(job, 'cancelled')
        self.assertEqual(self.store.get(job.id).status, 'interrupted')

    def test_terminal_playlist_children_and_parent_are_removed(self):
        group = self.submit(playlistMode='all')
        for index in range(2):
            self.manager._spawn_child(group, {'index': index, 'url': 'https://example.com/item'}, index)
        children = [j for j in self.manager.list_jobs() if j.group_id == group.id]
        self.manager._finish(children[0], 'complete')
        self.assertIsNone(self.store.get(children[0].id))
        self.assertIsNotNone(self.store.get(group.id))
        self.manager._finish(children[1], 'error', error='private error')
        self.assertIsNone(self.store.get(group.id))
        self.assertEqual(self.manager.clear_finished(), 3)
        self.assertEqual(self.manager.list_jobs(), [])

    def test_clear_and_dismiss_preserve_media_and_active_jobs(self):
        media = Path(self.temp.name) / 'file.mp4'
        partial = Path(self.temp.name) / 'file.mp4.part'
        media.write_bytes(b'media')
        partial.write_bytes(b'partial')
        done = self.submit()
        done.filename = str(media)
        self.manager._finish(done, 'complete')
        active = self.submit()
        with self.assertRaises(ValueError):
            self.manager.dismiss(active.id)
        self.assertEqual(self.manager.clear_finished(), 1)
        self.assertEqual([j.id for j in self.manager.list_jobs()], [active.id])
        self.assertEqual(media.read_bytes(), b'media')
        self.assertEqual(partial.read_bytes(), b'partial')

    def test_history_opt_in_persists_and_disabling_requires_existing_choice(self):
        self.store.configure_history(True)
        job = self.submit()
        self.manager._finish(job, 'error', error='private detail')
        self.assertIsNotNone(self.store.get(job.id))
        other = jobs.JobStore(self.db)
        self.addCleanup(other.close)
        self.assertTrue(other.history_policy()['retainFinishedHistory'])
        with self.assertRaises(ValueError):
            self.store.configure_history(False)
        self.assertTrue(self.store.history_policy()['retainFinishedHistory'])
        self.store.configure_history(False, existing='remove')
        self.assertIsNone(self.store.get(job.id))
        self.assertFalse(other.history_policy()['retainFinishedHistory'])

    def test_migration_detects_legacy_records_and_keep_does_not_opt_in(self):
        self.store.configure_history(True)
        self.store.upsert(jobs.Job(id='old', url='https://example.com', status='error', error='old detail'))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DROP TABLE history_policy')
        other = jobs.JobStore(self.db)
        self.addCleanup(other.close)
        policy = other.history_policy()
        self.assertTrue(policy['historyMigrationPending'])
        self.assertFalse(policy['retainFinishedHistory'])
        self.assertEqual(policy['retainedHistoryCount'], 1)
        other.configure_history(False, existing='keep')
        self.assertIsNotNone(other.get('old'))
        self.assertFalse(other.history_policy()['historyMigrationPending'])
        other.upsert(jobs.Job(id='new', url='https://example.com', status='complete'))
        self.assertIsNone(other.get('new'))
        other.configure_history(False, existing='remove')
        self.assertEqual(other.recent(), [])

    def test_delete_failure_does_not_report_success_or_drop_memory(self):
        self.store.configure_history(True)
        job = self.submit()
        self.manager._finish(job, 'error')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TRIGGER deny_delete BEFORE DELETE ON jobs BEGIN SELECT RAISE(ABORT, 'denied'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.manager.clear_finished()
        self.assertIn(job.id, [j.id for j in self.manager.list_jobs()])
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.configure_history(False, existing='remove')
        self.assertTrue(self.store.history_policy()['retainFinishedHistory'])
