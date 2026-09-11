import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
import app
import crm
from datetime import datetime, timezone
from email.utils import format_datetime


class JobTests(unittest.TestCase):
    def test_default_queries_target_sdet(self):
        self.assertIn('SDET', app.QUERIES)
        self.assertIn('QA Automation Engineer', app.QUERIES)
        self.assertNotIn('HPC', app.QUERIES)
    def test_ranking_prefers_specialty(self):
        hpc = app.rank('Разработчик C++', 'Linux OpenMP MPI sparse численные методы')
        self.assertEqual(hpc[1], 'C++ / HPC')
        self.assertGreater(hpc[0], app.rank('Frontend JavaScript','React CSS')[0])
        self.assertNotIn('arm', app.rank('Python','pharmacy Charm')[2])
        self.assertIn('c++', app.rank('С++ разработчик','')[2])

    def test_urls(self):
        self.assertEqual(app.canonical_url('https://nn.hh.ru/vacancy/123?from=search'), 'https://hh.ru/vacancy/123')
        for url in ('javascript:alert(1)', 'https://hh.ru.evil.org/vacancy/1', 'https://hh.ru/search/vacancy', 'https://user@hh.ru/vacancy/1'):
            with self.assertRaises(ValueError):
                app.canonical_url(url)

    def test_preserve_application_on_refresh(self):
        db = app.connect(':memory:')
        job = dict(url='https://hh.ru/vacancy/1',title='C++',description='Linux')
        app.upsert(db,job)
        db.execute("UPDATE jobs SET state='Отклик',notes='Заметка',followup='2026-10-01'")
        db.commit()
        app.upsert(db,{**job,'title':'C++ HPC'})
        row = db.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual((row['state'],row['notes'],row['followup']), ('Отклик','Заметка','2026-10-01'))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)
        app.upsert(db,{**job,'description':'Краткое описание из RSS. Python'})
        self.assertEqual(db.execute('SELECT description FROM jobs').fetchone()[0],'Linux')
        db.close()

    def test_pagination_deduplication_and_full_text(self):
        calls=[]
        def fake(path,params,contact):
            calls.append((path,params.copy()))
            if path == 'vacancies':
                return {'pages':2,'found':2,'items':[{'id':'1','alternate_url':'https://hh.ru/vacancy/1','name':'Engineer'}]}
            return {'description':'C++ Linux OpenMP','key_skills':[{'name':'MPI'}]}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'jobs.db'
            with patch.object(app,'hh_get',side_effect=fake),patch.object(app.time,'sleep'):
                app.collect(['HPC','OpenMP'],'113',True,'dev@example.org',lambda m:None,path)
            db=app.connect(path)
            rows=db.execute('SELECT * FROM jobs').fetchall()
            self.assertEqual(len(rows),1)
            self.assertIn('OpenMP',rows[0]['description'])
            self.assertGreater(rows[0]['score'],50)
            db.close()
        searches=[p for name,p in calls if name=='vacancies']
        self.assertEqual([p['page'] for p in searches],[0,1,0,1])
        self.assertTrue(all(p['work_format']=='REMOTE' for p in searches))

    def test_rate_limit_stops(self):
        with patch.object(app,'urlopen',side_effect=HTTPError('https://api.hh.ru',429,'Rate limit',{},None)):
            with self.assertRaisesRegex(RuntimeError,'429'):
                app.hh_get('vacancies',{},'dev@example.org')

    def test_salary_keeps_tax_basis(self):
        self.assertIn('до налогов',app.salary_text({'from':200000,'currency':'RUR','gross':True}))
        self.assertIn('на руки',app.salary_text({'to':200000,'currency':'RUR','gross':False}))
        self.assertEqual(app.salary_text(None),'Не указана')

    def test_habr_feed_recency_and_location_uncertainty(self):
        now = format_datetime(datetime.now(timezone.utc))
        feed = f'''<rss><channel><item><title>C++ developer</title>
        <link>https://career.habr.com/vacancies/123</link><author>Company</author>
        <description>Linux OpenMP</description><pubDate>{now}</pubDate></item>
        <item><title>Old</title><pubDate>Thu, 01 Jan 2015 12:00:00 +0000</pubDate>
        <link>https://career.habr.com/vacancies/124</link></item></channel></rss>'''
        jobs=app.parse_habr_feed(feed.encode(),False)
        self.assertEqual(len(jobs),1)
        self.assertIn('подтвердить офис',jobs[0]['location'])
        self.assertIn('Краткое описание',jobs[0]['description'])
        self.assertIn('Удалёнка',app.parse_habr_feed(feed.encode(),True)[0]['location'])

    def test_crm_migrates_statuses_and_keeps_history(self):
        db=app.connect(':memory:')
        app.upsert(db,dict(url='https://hh.ru/vacancy/7',title='SDET',description='Python Jenkins'))
        crm.set_state(db,'https://hh.ru/vacancy/7','На рассмотрении','Отправлен отклик','2026-09-20')
        crm.set_state(db,'https://hh.ru/vacancy/7','Пригласили на собеседование','Встреча назначена','')
        history=db.execute('SELECT previous,state FROM status_history').fetchall()
        self.assertEqual([(r['previous'],r['state']) for r in history],[('Новая','На рассмотрении'),('На рассмотрении','Пригласили на собеседование')])
        self.assertIn('Дошли до собеседования / оффера: 0',crm.hiring_summary(db))
        crm.update_fields(db,'https://hh.ru/vacancy/7',{'application_date':'2026-09-12'})
        self.assertIn('Дошли до собеседования / оффера: 1',crm.hiring_summary(db))
        db.close()

    def test_draft_and_unsent_email(self):
        job={'title':'SDET / QA Automation Engineer','company':'Example','url':'https://hh.ru/vacancy/9',
             'description':'Python Jenkins CI/CD автоматизация тестирования'}
        subject,body,basis=crm.draft(job)
        self.assertIn('SDET',subject)
        self.assertIn('@kiruhamage',body)
        self.assertIn('python',basis)
        raw=crm.email_bytes('hr@example.com',subject,body)
        self.assertIn(b'X-Unsent: 1',raw)
        with self.assertRaises(ValueError): crm.email_bytes('not-an-email',subject,body)


if __name__=='__main__':
    unittest.main()

