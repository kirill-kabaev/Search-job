"""Vacancy CRM, evidence-based drafts and email export; no message sending."""
import hashlib
import json
import re
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from html import unescape
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

STATES = ('Новая', 'На рассмотрении', 'Отклик принят', 'Отказ',
          'Пригласили на собеседование', 'Оффер', 'Не подходит')
CONTACT = {'name':'Кирилл Кабаев', 'telegram':'@kiruhamage',
           'phone':'+79506252658', 'email':'k.kabaev94@gmail.com'}
FIELDS = {
    'company_url':'Сайт компании', 'company_info':'О компании',
    'requisites':'Юрлицо / ИНН / ОГРН / адрес (если опубликованы)',
    'company_source':'Источник сведений о компании',
    'contact_name':'Контактное лицо', 'contact_email':'Email получателя',
    'contact_phone':'Телефон', 'contact_telegram':'Telegram',
    'contacts_source':'Источник контактов',
    'external_hiring_stats':'Публичная статистика найма (если доступна)',
    'external_stats_source':'Источник и период публичной статистики',
    'application_date':'Дата отправленного отклика (ГГГГ-ММ-ДД)',
    'cover_subject':'Тема письма', 'cover_body':'Текст письма',
    'cover_basis':'Основа письма', 'details_source':'Источник полного описания',
    'details_checked':'Дата проверки сведений', 'state_changed_at':'Дата изменения статуса'}


def now():
    return datetime.now().isoformat(timespec='seconds')


def migrate(db):
    columns={r[1] for r in db.execute('PRAGMA table_info(jobs)')}
    for field in FIELDS:
        if field not in columns:
            db.execute(f"ALTER TABLE jobs ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
    db.execute('''CREATE TABLE IF NOT EXISTS status_history (
        id INTEGER PRIMARY KEY, url TEXT NOT NULL, previous TEXT, state TEXT NOT NULL,
        changed_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '')''')
    mapping={'Отобрана':'Новая','Отклик':'На рассмотрении','HR':'Отклик принят',
             'Техническое':'Пригласили на собеседование'}
    for old,new in mapping.items():
        for r in db.execute('SELECT url FROM jobs WHERE state=?',(old,)).fetchall():
            db.execute('INSERT INTO status_history(url,previous,state,changed_at,note) VALUES (?,?,?,?,?)',
                       (r['url'],old,new,now(),'Перенос старого статуса; дата события неизвестна'))
        db.execute('UPDATE jobs SET state=? WHERE state=?',(new,old))
    db.commit()


def update_fields(db,url,values):
    if not set(values).issubset(FIELDS):
        raise ValueError('Неизвестное поле карточки')
    date=values.get('application_date','')
    if date:
        datetime.strptime(date,'%Y-%m-%d')
    if values:
        db.execute('UPDATE jobs SET '+','.join(f'{k}=?' for k in values)+' WHERE url=?',
                   (*values.values(),url))
        db.commit()


def set_state(db,url,state,notes,followup):
    if state not in STATES:
        raise ValueError('Неизвестный статус')
    if followup:
        datetime.strptime(followup,'%Y-%m-%d')
    old=db.execute('SELECT state FROM jobs WHERE url=?',(url,)).fetchone()
    if old is None:
        raise ValueError('Вакансия не найдена')
    stamp=now()
    if old['state']!=state:
        db.execute('INSERT INTO status_history(url,previous,state,changed_at) VALUES (?,?,?,?)',
                   (url,old['state'],state,stamp))
        db.execute('UPDATE jobs SET state_changed_at=? WHERE url=?',(stamp,url))
    db.execute('UPDATE jobs SET state=?,notes=?,followup=? WHERE url=?',(state,notes,followup,url))
    db.commit()


def hiring_summary(db,company=None):
    rows=db.execute('SELECT * FROM jobs'+(' WHERE company=?' if company else ''),
                    (company,) if company else ()).fetchall()
    submitted=[r for r in rows if r['application_date']]
    def reached(r,states):
        if r['state'] in states:
            return True
        return bool(db.execute('SELECT 1 FROM status_history WHERE url=? AND state IN ('+
            ','.join('?' for _ in states)+') LIMIT 1',(r['url'],*states)).fetchone())
    interviews=sum(reached(r,('Пригласили на собеседование','Оффер')) for r in submitted)
    offers=sum(reached(r,('Оффер',)) for r in submitted)
    lines=[f'Личная воронка: {company or "все компании"}',f'Вакансий в базе: {len(rows)}',
        f'Отправлено откликов с указанной датой: {len(submitted)}',
        f'Дошли до собеседования / оффера: {interviews}',f'Офферов: {offers}']
    if submitted:
        lines.extend((f'Доля дошедших до собеседования: {interviews/len(submitted):.0%}',
                      f'Доля офферов: {offers/len(submitted):.0%}'))
    else:
        lines.append('Конверсия не рассчитана: нет дат отправки откликов.')
    lines.append('\nТекущие статусы:')
    lines.extend(f'{s}: {sum(r["state"]==s for r in rows)}' for s in STATES)
    lines.append('\nЭто ваши записи, не общая статистика найма работодателя. '
                 'Дубли между площадками считаются отдельно; знаменатель — записи с датой отклика. '
                 'Свежие отклики могут ещё не получить ответ.')
    return '\n'.join(lines)


EXPERIENCE = (
    (('python','sdet','qa','автоматизац','тестирован','testing'),
     'Разрабатывал тестовые инструменты на Python/C++ и спроектировал фреймворк проверки '
     'профилировщиков, использующий семантические связи и статистические критерии вместо фиксированных эталонов.'),
    (('c++','openmp','mpi','hpc','численн','sparse'),
     'В Huawei я разрабатывал и оптимизировал на C++17 алгоритмы для разреженных матриц, '
     'работал с OpenMP/MPI, алгоритмами AMD/QAMD и Nested Dissection.'),
    (('vtune','valgrind','performance','профилиров','производительност','numa'),
     'Занимался профилированием и анализом регрессий производительности с VTune и Valgrind, '
     'работал с метриками ARM/x86 и NUMA.'),
    (('jenkins','ci/cd','linux'),
     'Автоматизировал тестирование и бенчмаркинг в Jenkins под Linux.')
)


def draft(job):
    """Only fixed, resume-grounded claims; vacancy text never becomes instructions."""
    text=(job['title']+' '+job['description']).lower().replace('с++','c++')
    selected=[]
    matches=[]
    for keys,claim in EXPERIENCE:
        hits=[k for k in keys if re.search(r'(?<!\w)'+re.escape(k)+r'(?!\w)',text) if k.isascii()]
        hits += [k for k in keys if not k.isascii() and k in text]
        if hits:
            selected.append(claim); matches.extend(hits)
    title=job['title'].split('\n')[0].strip()
    m=re.search(r'Требуется «(.+?)»',title)
    title=m[1] if m else title
    company=job['company'].strip()
    intro=f'Меня заинтересовала вакансия «{title}»'+(f' в компании {company}.' if company else '.')
    if not selected:
        selected=['Мой опыт включает промышленную разработку на C++ и Python в Huawei, '
                  'численные алгоритмы и разработку тестовых инструментов под Linux.']
    body='\n\n'.join(['Здравствуйте!',intro,' '.join(selected[:3]),
        'Готов подробнее рассказать о проектах, своём вкладе и способах проверки корректности результатов. '
        'Рассматриваю офис в Нижнем Новгороде или полностью удалённую работу.',
        f'Вакансия: {job["url"]}',
        f'С уважением,\n{CONTACT["name"]}\nTelegram: {CONTACT["telegram"]}\n'
        f'Телефон: {CONTACT["phone"]}\nEmail: {CONTACT["email"]}'])
    basis='Совпадения: '+(', '.join(dict.fromkeys(matches)) or 'нет; общий черновик')
    if job['description'].startswith('Краткое описание из RSS.'):
        basis+='\nТолько RSS: загрузите полные требования и пересоздайте письмо для лучшей адаптации.'
    basis+='\nВыбраны подтверждённые фрагменты опыта, а не заявлено соответствие всем требованиям.'
    return f'Отклик: {title} — Кирилл Кабаев',body,basis


def email_bytes(to,subject,body,attachment=None):
    if not re.fullmatch(r'[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+',to):
        raise ValueError('Укажите один корректный email получателя')
    if not subject.strip() or any(c in subject for c in '\r\n'):
        raise ValueError('Нужна тема письма в одну строку')
    if not body.strip():
        raise ValueError('Письмо пустое')
    msg=EmailMessage(policy=SMTP)
    msg['From']=f'{CONTACT["name"]} <{CONTACT["email"]}>'
    msg['To']=to
    msg['Subject']=subject
    msg['X-Unsent']='1'
    msg.set_content(body)
    if attachment:
        p=Path(attachment)
        if p.suffix.lower()!='.pdf':
            raise ValueError('Для вложения выберите PDF-резюме')
        if p.stat().st_size>15_000_000:
            raise ValueError('PDF больше 15 МБ')
        msg.add_attachment(p.read_bytes(),maintype='application',subtype='pdf',filename=p.name)
    return msg.as_bytes()


def extract_jobposting(html):
    for raw in re.findall(r'<script[^>]*type=[\"\']application/ld\+json[\"\'][^>]*>(.*?)</script>',html,re.S|re.I):
        try:
            data=json.loads(raw)
        except ValueError:
            continue
        stack=data if isinstance(data,list) else [data]
        while stack:
            value=stack.pop()
            if isinstance(value,dict):
                if value.get('@type')=='JobPosting':
                    return value
                stack.extend(value.get('@graph',[]))
    raise ValueError('Полное описание JobPosting не найдено. Добавьте текст вручную.')


def habr_details(url,clean):
    p=urlparse(url)
    if p.scheme!='https' or p.netloc!='career.habr.com' or not re.fullmatch(r'/vacancies/\d+',p.path):
        raise ValueError('Нужна ссылка на вакансию Хабр Карьеры')
    with urlopen(Request(url,headers={'User-Agent':'KirillJobSearch/2.0'}),timeout=25) as r:
        html=r.read(5_000_001)
    if len(html)>5_000_000:
        raise ValueError('Ответ слишком большой')
    data=extract_jobposting(html.decode('utf-8'))
    org=data.get('hiringOrganization') or {}
    description=clean(data.get('description',''))
    company_info=clean(org.get('description',''))
    about=re.search(r'<h[1-6][^>]*>[^<]*(?:О компании|О нас|О команде)[\s\S]*?</h[1-6]>(.*?)(?=<h[1-6]|$)',data.get('description',''),re.I|re.S)
    if not company_info and about:
        company_info=clean(about[1]).strip()
    # Only published structured contacts; never guess an email from a name/domain.
    values={'company_url':org.get('sameAs',''),'company_info':company_info,
        'company_source':url,'details_source':url,'details_checked':now()}
    if org.get('email'):
        values.update(contact_email=org['email'],contacts_source=url)
    if org.get('telephone'):
        values.update(contact_phone=org['telephone'],contacts_source=url)
    if org.get('legalName') or org.get('taxID'):
        values['requisites']='; '.join(filter(None,(org.get('legalName'),org.get('taxID'))))
    return {'title':data.get('title',''), 'description':description,
            'company':org.get('name','')}, {k:v for k,v in values.items() if v}


def open_card(root,db,url,refresh,upsert,clean,hh_get,contact_email):
    import tkinter as tk
    from tkinter import ttk,messagebox,filedialog
    import queue
    import threading
    win=tk.Toplevel(root)
    win.title('Карточка вакансии · компания · контакты · письмо')
    win.geometry('1050x800')
    job=dict(db.execute('SELECT * FROM jobs WHERE url=?',(url,)).fetchone())
    ttk.Label(win,text=job['title'],wraplength=980,padding=10).pack(fill='x')
    ttk.Label(win,text=f'Источник / ID: {url}',padding=(10,0)).pack(fill='x')
    book=ttk.Notebook(win); book.pack(fill='both',expand=True,padx=10,pady=8)
    widgets={}
    for title,keys in (
        ('Компания',('company_url','company_info','requisites','company_source')),
        ('Контакты',('contact_name','contact_email','contact_phone','contact_telegram','contacts_source','application_date')),
        ('Публичная статистика',('external_hiring_stats','external_stats_source'))):
        panel=ttk.Frame(book,padding=12); book.add(panel,text=title)
        ttk.Label(panel,text='Пустое поле означает: сведения не указаны или не найдены. Не подставляйте предположения.',wraplength=950).pack(anchor='w',pady=6)
        for key in keys:
            ttk.Label(panel,text=FIELDS[key]).pack(anchor='w',pady=(6,0))
            if key in ('company_info','requisites','external_hiring_stats','external_stats_source'):
                w=tk.Text(panel,height=5 if key=='company_info' else 3,wrap='word')
                w.insert('1.0',job[key])
            else:
                w=ttk.Entry(panel); w.insert(0,job[key])
            w.pack(fill='x'); widgets[key]=w
    description_panel=ttk.Frame(book,padding=10); book.add(description_panel,text='Требования')
    ttk.Label(description_panel,text='Полное описание вакансии. Можно загрузить с площадки или вставить вручную.').pack(anchor='w')
    description=tk.Text(description_panel,wrap='word'); description.pack(fill='both',expand=True)
    description.insert('1.0',job['description'])
    stats_panel=ttk.Frame(book,padding=10); book.add(stats_panel,text='Моя воронка и история')
    stats=tk.Text(stats_panel,wrap='word'); stats.pack(fill='both',expand=True)
    letter_panel=ttk.Frame(book,padding=10); book.add(letter_panel,text='Письмо')
    ttk.Label(letter_panel,text='Проверьте адрес, требования и текст. Экспорт создаёт файл и ничего не отправляет.',wraplength=950).pack(anchor='w')
    subject=ttk.Entry(letter_panel); subject.pack(fill='x',pady=8); subject.insert(0,job['cover_subject'])
    body=tk.Text(letter_panel,wrap='word',height=14); body.pack(fill='both',expand=True); body.insert('1.0',job['cover_body'])
    basis=tk.StringVar(value=job['cover_basis'])
    ttk.Label(letter_panel,textvariable=basis,wraplength=950).pack(fill='x',pady=6)
    attachment=tk.StringVar()
    ttk.Label(letter_panel,textvariable=attachment,wraplength=950).pack(fill='x')
    actions=ttk.Frame(letter_panel); actions.pack(fill='x',pady=8)
    status=tk.StringVar(value='Дату отклика укажите после фактической отправки. Сохранение письма её не меняет.')
    ttk.Label(win,textvariable=status,wraplength=1000,padding=8).pack(fill='x')

    def value(w):
        return w.get('1.0','end-1c').strip() if isinstance(w,tk.Text) else w.get().strip()

    def display(w,text):
        if isinstance(w,tk.Text):
            w.delete('1.0','end'); w.insert('1.0',text)
        else:
            w.delete(0,'end'); w.insert(0,text)

    def update_stats():
        stats.delete('1.0','end')
        stats.insert('end',hiring_summary(db,job['company'])+'\n\nИстория этой вакансии:\n')
        for h in db.execute('SELECT * FROM status_history WHERE url=? ORDER BY id',(url,)):
            stats.insert('end',f"{h['changed_at']} · {h['previous']} → {h['state']} · {h['note']}\n")
        stats.insert('end','\nОпубликована: '+job['published']+'\nИсточник описания: '+job['details_source']+
                     '\nПроверено: '+job['details_checked'])

    def save():
        try:
            values={key:value(w) for key,w in widgets.items()}
            values.update(cover_subject=subject.get(),cover_body=body.get('1.0','end-1c'),cover_basis=basis.get())
            update_fields(db,url,values)
            full=dict(db.execute('SELECT * FROM jobs WHERE url=?',(url,)).fetchone())
            full['description']=description.get('1.0','end-1c')
            upsert(db,full)
            job.update(dict(db.execute('SELECT * FROM jobs WHERE url=?',(url,)).fetchone()))
        except (ValueError,OSError) as e:
            messagebox.showerror('Карточка',str(e),parent=win); return False
        refresh(); update_stats(); status.set('Карточка и черновик сохранены.'); return True

    def generate():
        if body.get('1.0','end-1c').strip() and not messagebox.askyesno('Пересоздать письмо','Заменить текущий текст новым черновиком?',parent=win):
            return
        if not save():
            return
        sub,text,why=draft(job)
        display(subject,sub); display(body,text); basis.set(why)
        save()

    def choose_attachment():
        p=filedialog.askopenfilename(parent=win,title='PDF-резюме для письма',filetypes=[('PDF','*.pdf')])
        if p: attachment.set(p)

    def export(kind):
        if not save():
            return
        try:
            if kind=='eml':
                data=email_bytes(value(widgets['contact_email']),subject.get(),body.get('1.0','end-1c'),attachment.get() or None)
            else:
                data=(subject.get()+'\n\n'+body.get('1.0','end-1c')).encode('utf-8')
            path=filedialog.asksaveasfilename(parent=win,title='Сохранить письмо',defaultextension='.'+kind,
                initialfile='Отклик_'+url.rsplit('/',1)[-1]+'.'+kind,filetypes=[(kind.upper(),'*.'+kind)])
            if path:
                Path(path).write_bytes(data); status.set('Сохранено: '+path+'. Отправка не выполнялась.')
        except (ValueError,OSError) as e:
            messagebox.showerror('Экспорт письма',str(e),parent=win)

    results=queue.Queue()
    def enrich():
        if not save(): return
        enrich_button.configure(state='disabled'); status.set('Загружаю опубликованные сведения…')
        def worker():
            try:
                if 'career.habr.com' in url:
                    payload=habr_details(url,clean)
                else:
                    v=hh_get('vacancies/'+url.rsplit('/',1)[-1],{},contact_email)
                    employer=v.get('employer') or {}; c=v.get('contacts') or {}
                    meta={'company_source':url,'details_source':url,'details_checked':now()}
                    if c:
                        meta.update(contact_name=c.get('name') or '',contact_email=c.get('email') or '',
                            contact_phone='; '.join(' '.join(str(p.get(k) or '') for k in ('country','city','number')) for p in c.get('phones',[])),contacts_source=url)
                    if employer.get('id'):
                        e=hh_get('employers/'+employer['id'],{},contact_email)
                        meta.update(company_url=e.get('site_url') or '',company_info=clean(e.get('description','')),
                            company_source=e.get('alternate_url') or url)
                        if e.get('open_vacancies') is not None:
                            meta.update(external_hiring_stats=f"Открытых вакансий на hh: {e['open_vacancies']}. Это не число наймов.",
                                        external_stats_source=meta['company_source']+'; проверено '+now())
                    payload=({'title':v['name'],'description':clean(v.get('description','')),
                              'company':employer.get('name','')},{k:v for k,v in meta.items() if v})
                results.put(payload)
            except Exception as e:
                results.put(e)
        threading.Thread(target=worker,daemon=True).start()
        win.after(150,poll)

    def poll():
        try: result=results.get_nowait()
        except queue.Empty:
            win.after(150,poll); return
        enrich_button.configure(state='normal')
        if isinstance(result,Exception):
            status.set('Загрузка не выполнена: '+str(result)); return
        main_values,extras=result
        latest=dict(db.execute('SELECT * FROM jobs WHERE url=?',(url,)).fetchone())
        latest.update({k:v for k,v in main_values.items() if v})
        upsert(db,latest)
        extras={k:v for k,v in extras.items() if not latest.get(k) or k in ('details_source','details_checked')}
        update_fields(db,url,extras)
        job.update(dict(db.execute('SELECT * FROM jobs WHERE url=?',(url,)).fetchone()))
        for k,w in widgets.items(): display(w,job[k])
        display(description,job['description']); update_stats(); refresh()
        status.set('Доступные сведения загружены. Пустые контакты/реквизиты не опубликованы в полученных данных. Пересоздайте письмо при изменении требований.')

    ttk.Button(actions,text='Составить / пересоздать',command=generate).pack(side='left')
    ttk.Button(actions,text='Прикрепить PDF',command=choose_attachment).pack(side='left',padx=5)
    ttk.Button(actions,text='Убрать вложение',command=lambda:attachment.set('')).pack(side='left')
    ttk.Button(actions,text='Экспорт .eml',command=lambda:export('eml')).pack(side='left',padx=5)
    ttk.Button(actions,text='Экспорт .txt',command=lambda:export('txt')).pack(side='left')
    bottom=ttk.Frame(win,padding=10); bottom.pack(fill='x')
    ttk.Button(bottom,text='Сохранить карточку и письмо',command=save).pack(side='left')
    enrich_button=ttk.Button(bottom,text='Загрузить полное описание и компанию',command=enrich)
    enrich_button.pack(side='left',padx=8)
    update_stats()
    return win

