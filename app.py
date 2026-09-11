"""Personal SDET / QA Automation job search assistant. Python 3.10+, standard library only."""
import json
import os
import queue
import re
import sqlite3
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
import crm
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parent
STATES = crm.STATES
QUERIES = ('SDET', 'QA Automation Engineer', 'Python автоматизация тестирования',
           'Python тестировщик', 'Jenkins CI/CD', 'тестовые фреймворки')
PROFILES = {
    'C++ / HPC': {'c++': 24, 'linux': 10, 'openmp': 14, 'mpi': 12, 'hpc': 12,
                  'численн': 10, 'алгоритм': 8, 'sparse': 10, 'vtune': 10,
                  'профилиров': 10, 'производительност': 8, 'python': 4},
    'SDET / Performance': {'python': 20, 'linux': 10, 'sdet': 20,
                  'автоматизац': 12, 'тестирован': 10, 'jenkins': 12,
                  'c++': 8, 'профилиров': 10, 'performance': 10,
                  'numa': 10, 'arm': 5, 'x86': 5}}


def clean(value):
    value = re.sub(r'</(?:p|li|div|h[1-6])>|<br\s*/?>', '\n', value or '', flags=re.I)
    return unescape(re.sub(r'<[^>]*>', ' ', value))


def rank(title, description):
    """Explainable keyword heuristic, never an interview probability."""
    text = clean(title + ' ' + description).lower().replace('с++', 'c++')
    results = []
    for profile, weights in PROFILES.items():
        hits = [k for k in weights if (re.search(r'(?<!\w)' + re.escape(k) + r'(?!\w)', text)
                if k.isascii() else k in text)]
        results.append((min(100, sum(weights[k] for k in hits)), profile, ', '.join(hits)))
    return max(results)


def canonical_url(url):
    p = urlparse(url.strip())
    host = (p.hostname or '').lower()
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Нужна HTTPS-ссылка на вакансию hh.ru или career.habr.com')
    if host == 'hh.ru' or host.endswith('.hh.ru'):
        m = re.fullmatch(r'/vacancy/(\d+)/?', p.path)
        if m:
            return 'https://hh.ru/vacancy/' + m[1]
    if host == 'career.habr.com':
        m = re.fullmatch(r'/vacancies/(\d+)/?', p.path)
        if m:
            return 'https://career.habr.com/vacancies/' + m[1]
    raise ValueError('Вставьте ссылку на конкретную вакансию, а не страницу поиска')


def connect(path=None):
    db = sqlite3.connect(path or BASE / 'jobs.sqlite3')
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS jobs (
      url TEXT PRIMARY KEY, title TEXT, company TEXT, description TEXT,
      salary TEXT, location TEXT, published TEXT, score INTEGER, profile TEXT,
      reasons TEXT, state TEXT DEFAULT 'Новая', notes TEXT DEFAULT '',
      followup TEXT DEFAULT '', first_seen TEXT, last_seen TEXT)''')
    db.commit()
    crm.migrate(db)
    return db


def upsert(db, job):
    job = dict(job)
    job['url'] = canonical_url(job['url'])
    old = db.execute('SELECT * FROM jobs WHERE url=?', (job['url'],)).fetchone()
    if old and job['description'].startswith('Краткое описание из RSS.') and not old['description'].startswith('Краткое описание из RSS.'):
        for field in ('title', 'description', 'salary'):
            job[field] = old[field]
    score, profile, reasons = rank(job['title'], job['description'])
    now = datetime.now().isoformat(timespec='seconds')
    db.execute('''INSERT INTO jobs
      (url,title,company,description,salary,location,published,score,profile,reasons,first_seen,last_seen)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(url) DO UPDATE SET title=excluded.title, company=excluded.company,
      description=excluded.description, salary=excluded.salary, location=excluded.location,
      published=excluded.published, score=excluded.score, profile=excluded.profile,
      reasons=excluded.reasons, last_seen=excluded.last_seen''',
      (canonical_url(job['url']), job['title'], job.get('company', ''), clean(job['description']),
       job.get('salary', 'Не указана'), job.get('location', ''), job.get('published', ''),
       score, profile, reasons, now, now))
    db.commit()


def salary_text(s):
    if not s:
        return 'Не указана'
    amount = ' — '.join(str(s.get(k) or '…') for k in ('from', 'to'))
    tax = {True: 'до налогов', False: 'на руки'}.get(s.get('gross'), 'налоги не уточнены')
    return f"{amount} {s.get('currency', '')} ({tax})"


def hh_get(path, params, contact):
    req = Request('https://api.hh.ru/' + path + '?' + urlencode(params),
                  headers={'HH-User-Agent': f'KirillJobSearch/1.0 ({contact})',
                           'User-Agent': f'KirillJobSearch/1.0 ({contact})'})
    try:
        with urlopen(req, timeout=25) as r:
            return json.load(r)
    except HTTPError as e:
        if e.code in (403, 429):
            raise RuntimeError(f'hh API: HTTP {e.code}. Поиск остановлен. Повторите позже или откройте сайт.') from e
        raise


def collect(queries, area, remote, contact, emit, db_path=None):
    db = connect(db_path)
    seen = set()
    try:
        for query in queries:
            # Bounded to 100 recent results per query; report truncation explicitly.
            for page in range(2):
                params = dict(text=query, period=14, per_page=50, page=page, order_by='publication_time')
                if area:
                    params['area'] = area
                params['work_format'] = 'REMOTE' if remote else 'ON_SITE'
                result = hh_get('vacancies', params, contact)
                if page == 0:
                    emit(f'{query}: найдено {result.get("found", 0)}; загружаю до 100')
                for v in result.get('items', []):
                    if v['id'] in seen:
                        continue
                    seen.add(v['id'])
                    snippet = v.get('snippet') or {}
                    desc = '\n'.join(filter(None, (snippet.get('requirement'), snippet.get('responsibility'))))
                    # Search returns excerpts. Fetch full description to rank actual requirements.
                    time.sleep(0.25)
                    detail = hh_get('vacancies/' + v['id'], {}, contact)
                    if detail.get('archived'):
                        continue
                    desc = detail.get('description') or desc
                    desc += '\n' + ', '.join(k['name'] for k in detail.get('key_skills', []))
                    upsert(db, dict(url=v['alternate_url'], title=v['name'],
                        company=(v.get('employer') or {}).get('name', ''), description=desc,
                        salary=salary_text(v.get('salary')), location=(v.get('area') or {}).get('name', ''),
                        published=v.get('published_at', '')))
                if page + 1 >= result.get('pages', 0):
                    break
                time.sleep(0.3)
        emit(f'Готово: обработано {len(seen)} уникальных вакансий. Повторный поиск сохраняет заметки и статусы.')
    finally:
        db.close()


def parse_habr_feed(data, is_remote):
    result = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    for item in ET.fromstring(data).findall('./channel/item'):
        pub = item.findtext('pubDate', '')
        if pub:
            stamp = parsedate_to_datetime(pub)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp < cutoff:
                continue
            pub = stamp.isoformat()
        summary = clean(item.findtext('description', ''))
        title = item.findtext('title', '')
        result.append(dict(url=canonical_url(item.findtext('link', '')), title=title,
            description='Краткое описание из RSS. Полные требования проверьте по ссылке.\n' + summary,
            company=item.findtext('author',''), published=pub,
            salary='См. описание; налоги уточнить',
            location='Удалёнка: уточнить ограничения' if is_remote else 'Нижний Новгород: подтвердить офис, исключить обязательный гибрид'))
    return result


def collect_habr(queries, streams, emit, db_path=None):
    db = connect(db_path)
    seen = set()
    try:
        for _, is_remote in streams:
            for query in queries:
                params = {'q':query, 'page':1, 'per_page':25}
                params.update({'remote':1} if is_remote else {'city_id':715})
                req = Request('https://career.habr.com/vacancies/rss?' + urlencode(params),
                              headers={'User-Agent':'KirillJobSearch/1.0'})
                with urlopen(req,timeout=25) as response:
                    data = response.read(5_000_001)
                if len(data)>5_000_000:
                    raise ValueError('Слишком большой RSS-ответ')
                items = parse_habr_feed(data,is_remote)
                for job in items:
                    # Prefer explicit remote metadata if same job occurs in both streams.
                    upsert(db,job)
                    seen.add(job['url'])
                emit(f'Хабр RSS · {query}: {len(items)} записей за 14 дней. Лента может быть неполной.')
                time.sleep(0.5)
        emit(f'Хабр: сохранено {len(seen)} уникальных вакансий. В НН проверьте офисный формат; балл рассчитан по RSS.')
    finally:
        db.close()


def main():
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog
    root = tk.Tk()
    root.title('Поиск работы · SDET / QA Automation')
    root.geometry('1280x850')
    db = connect()
    events = queue.Queue()
    top = ttk.Frame(root, padding=12)
    top.pack(fill='x')
    ttk.Label(top, text='Поисковые запросы через ;').grid(row=0, column=0, sticky='w')
    queries = tk.StringVar(value='; '.join(QUERIES))
    ttk.Entry(top, textvariable=queries, width=115).grid(row=0, column=1, columnspan=5, sticky='ew')
    ttk.Label(top, text='Формат работы').grid(row=1, column=0, sticky='w')
    mode = tk.StringVar(value='Офис НН + удалёнка')
    ttk.Combobox(top, textvariable=mode, values=('Офис НН + удалёнка', 'Только офис НН', 'Только удалёнка'), state='readonly', width=23).grid(row=1, column=1)
    ttk.Label(top, text='Офис НН или удалёнка').grid(row=1, column=2)
    ttk.Label(top, text='Email для заголовка hh API').grid(row=1, column=3)
    contact = tk.StringVar(value=os.environ.get('HH_CONTACT_EMAIL', crm.CONTACT['email']))
    ttk.Entry(top, textvariable=contact, width=30).grid(row=1, column=4)
    status = tk.StringVar(value='Выберите поиск. Балл — совпадение ключевых слов, а не вероятность оффера.')
    buttons = ttk.Frame(root, padding=(12, 0))
    buttons.pack(fill='x')
    table = ttk.Frame(root)
    table.pack(fill='both', expand=True, padx=12, pady=8)
    tree = ttk.Treeview(table, columns=('score', 'title', 'company', 'salary', 'state'), show='headings', height=14)
    for name, label, width in [('score','Балл',55),('title','Вакансия',330),('company','Компания',170),('salary','Зарплата',210),('state','Статус',220)]:
        tree.heading(name, text=label)
        tree.column(name, width=width)
    scroll = ttk.Scrollbar(table, orient='vertical', command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right',fill='y')
    tree.pack(side='left',fill='both', expand=True)
    details = tk.Text(root, height=12, wrap='word')
    details.pack(fill='both', expand=True, padx=12)
    edit = ttk.Frame(root, padding=12)
    edit.pack(fill='x')
    state = tk.StringVar(value='Новая')
    ttk.Combobox(edit, textvariable=state, values=STATES, state='readonly', width=29).pack(side='left')
    ttk.Label(edit, text='  Проверить после (ГГГГ-ММ-ДД): ').pack(side='left')
    followup = tk.StringVar()
    ttk.Entry(edit, textvariable=followup, width=12).pack(side='left')
    notes = tk.StringVar()
    ttk.Entry(edit, textvariable=notes, width=48).pack(side='left', padx=8)
    ttk.Label(root, textvariable=status, padding=12, wraplength=1200).pack(fill='x')

    def selected():
        ids = tree.selection()
        return db.execute('SELECT * FROM jobs WHERE url=?', (ids[0],)).fetchone() if ids else None

    def refresh():
        tree.delete(*tree.get_children())
        for j in db.execute('SELECT * FROM jobs ORDER BY score DESC, published DESC'):
            tree.insert('', 'end', iid=j['url'], values=(j['score'],j['title'],j['company'],j['salary'],j['state']))

    def show(_=None):
        j = selected()
        if not j:
            return
        state.set(j['state']); notes.set(j['notes']); followup.set(j['followup'])
        details.delete('1.0', 'end')
        details.insert('end', f"{j['title']}\n{j['url']}\n{j['profile']} · {j['score']}/100 · {j['reasons']}\n"
            f"{j['location']} · Опубликована: {j['published']} · Последняя загрузка: {j['last_seen']}\n"
            'Требования могут содержать обязательные навыки, которых нет в резюме. Проверьте текст.\n\n' + j['description'])

    def save():
        j = selected()
        if not j:
            return
        if followup.get():
            try:
                datetime.strptime(followup.get(), '%Y-%m-%d')
            except ValueError:
                messagebox.showerror('Дата', 'Введите дату ГГГГ-ММ-ДД'); return
        crm.set_state(db,j['url'],state.get(),notes.get(),followup.get())
        refresh(); status.set('Статус, заметка и дата сохранены. Изменение записано в историю.')

    def browse():
        j = selected()
        if j:
            webbrowser.open(canonical_url(j['url']))

    def card():
        j=selected()
        if j:
            crm.open_card(root,db,j['url'],refresh,upsert,clean,hh_get,contact.get())
        else:
            messagebox.showinfo('Карточка','Выберите вакансию в списке.')

    def browser_search(source):
        q = simpledialog.askstring('Поиск', 'Запрос:', initialvalue='C++ Linux')
        if q:
            url = ('https://nn.hh.ru/search/vacancy?' + urlencode({'text': q}) if source == 'hh'
                   else 'https://career.habr.com/vacancies?' + urlencode({'q': q, 'type': 'all'}))
            webbrowser.open(url)

    def manual():
        win = tk.Toplevel(root); win.title('Добавить вакансию hh / Хабр Карьеры')
        fields = {}
        for label, key in [('Ссылка','url'),('Название','title'),('Компания','company'),('Зарплата (как на сайте)','salary')]:
            ttk.Label(win,text=label).pack(anchor='w')
            fields[key] = ttk.Entry(win,width=85); fields[key].pack(fill='x')
        ttk.Label(win,text='Вставьте описание и требования для оценки совпадений').pack(anchor='w')
        text = tk.Text(win,width=85,height=18); text.pack()
        def commit():
            j = {k:v.get().strip() for k,v in fields.items()}
            j['description'] = text.get('1.0','end').strip()
            try:
                if not j['title'] or not j['description']:
                    raise ValueError('Нужны название и описание вакансии')
                upsert(db,j)
            except ValueError as e:
                messagebox.showerror('Проверьте данные',str(e),parent=win); return
            refresh(); win.destroy()
        ttk.Button(win,text='Добавить / обновить',command=commit).pack(pady=8)

    def search(source='hh'):
        qs = [s.strip() for s in queries.get().split(';') if s.strip()]
        email = contact.get().strip()
        if not qs or (source == 'hh' and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email)):
            messagebox.showerror('Параметры', 'Укажите запрос и контактный email для hh API.'); return
        chosen = mode.get()
        streams = []
        if chosen != 'Только удалёнка':
            streams.append(('66', False))
        if chosen != 'Только офис НН':
            streams.append(('', True))
        search_button.configure(state='disabled')
        habr_button.configure(state='disabled')
        status.set('Загружаю свежие вакансии за 14 дней. Можно продолжать работать со списком.')
        def worker():
            try:
                if source == 'habr':
                    collect_habr(qs, streams, events.put)
                else:
                    for city, is_remote in streams:
                        events.put('Поток: ' + ('удалёнка' if is_remote else 'офис, Нижний Новгород'))
                        collect(qs, city, is_remote, email, events.put)
            except Exception as e:
                events.put('Ошибка загрузки: ' + str(e) + '. Уже загруженные вакансии сохранены.')
            finally:
                events.put(None)
        threading.Thread(target=worker,daemon=True).start()

    def poll():
        try:
            while True:
                msg = events.get_nowait()
                if msg is None:
                    search_button.configure(state='normal'); refresh()
                    habr_button.configure(state='normal')
                else:
                    status.set(msg)
        except queue.Empty:
            pass
        root.after(200,poll)

    def due():
        rows = db.execute("SELECT title,followup FROM jobs WHERE followup<>'' AND followup<=? AND state NOT IN ('Отказ','Не подходит','Оффер')", (datetime.now().date().isoformat(),)).fetchall()
        messagebox.showinfo('Проверить отклики', '\n'.join(f"{r['followup']} · {r['title']}" for r in rows) or 'Нет откликов с наступившей датой проверки.')

    search_button = ttk.Button(buttons,text='Загрузить hh',command=search); search_button.pack(side='left')
    habr_button = ttk.Button(buttons,text='Загрузить Хабр RSS',command=lambda:search('habr')); habr_button.pack(side='left',padx=4)
    for label, action in [('Поиск на hh',lambda:browser_search('hh')),('Поиск на Хабр Карьере',lambda:browser_search('habr')),('Добавить по ссылке и тексту',manual),('Открыть вакансию',browse),('Проверить отклики',due)]:
        ttk.Button(buttons,text=label,command=action).pack(side='left',padx=4)
    extra=ttk.Frame(root,padding=(12,4))
    extra.pack(before=table,fill='x')
    ttk.Button(extra,text='Карточка компании, контакты и письмо',command=card).pack(side='left')
    ttk.Button(extra,text='Статистика моих откликов',command=lambda:messagebox.showinfo('Моя воронка',crm.hiring_summary(db))).pack(side='left',padx=8)
    ttk.Button(edit,text='Сохранить',command=save).pack(side='left')
    tree.bind('<<TreeviewSelect>>',show)
    tree.bind('<Double-1>',lambda _:card())
    refresh(); poll(); root.mainloop(); db.close()


if __name__ == '__main__':
    main()

