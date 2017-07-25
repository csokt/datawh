#!/usr/bin/env python2
# -*- coding: utf-8 -*-

import sys, os, time, collections, re, psycopg2, psycopg2.extras, psycopg2.extensions, xlrd, yaml, logging
psycopg2.extensions.register_type(psycopg2.extensions.UNICODE)
psycopg2.extensions.register_type(psycopg2.extensions.UNICODEARRAY)
#logging.basicConfig(filename='datawh.log', format='%(asctime)s %(levelname)s %(message)s', level=logging.DEBUG)
logging.basicConfig(filename='datawh.log', format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)

#from os import scandir, walk
from scandir import scandir, walk

RULES  = 'datawh_xls.yaml'
SUFFIX = ('.xls', '.xlsx')
FileDescr = collections.namedtuple('FileDescr', 'domain filetype filename path size mod_time')

def add_pos(pos1, pos2):
  return([sum(x) for x in zip(pos1, pos2)])

"""
def xls_search(sheet, search_value):
  for i in range(sheet.nrows):
    row = sheet.row_values(i)
    for j in range(len(row)):
      if row[j] == search_value:
##        print('talált', i,j)
        return (i,j)
  return None
"""
class DataWH():
  def init(self):
    with open(RULES, 'r') as yamlfile:
      self.xls_rules = yaml.load(yamlfile)
    self.CONF_PATH = self.xls_rules['params']['path']
    logging.debug(u'CONF_PATH: {}'.format(self.CONF_PATH))
    self.pg_files = {}
    self.conn = psycopg2.connect(self.xls_rules['params']['connect'])
    logging.debug(u'db connected.')
    self.cur = self.conn.cursor()
    self.cur.execute("""SELECT domain, filetype, filename, path, size, mod_time FROM datawh_files;""")
    for row in map(FileDescr._make, self.cur.fetchall()):
      key = (row.path,row.size,row.mod_time)
      self.pg_files[key] = True
    self.conn.commit()

  def dumps(self, doc):
    return yaml.safe_dump(doc, default_flow_style=False, allow_unicode=True)

  def insert_db(self, name):
    logging.info(u'Insert {} into datawh_documents table.'.format(self.label))
    if len(self.errors):
      logging.warning(u'{} document error: {}'.format(self.label, self.errors))
    query = "INSERT INTO datawh_documents (files_id, doctype, label, document, errors, create_date) VALUES (%s, %s, %s, %s, %s, now() at time zone 'utc');"
    self.cur.execute(query, (self.files_id, name, self.label, self.dumps(self.document), self.dumps(self.errors)))
    self.conn.commit()
    self.label, self.document, self.errors = '', {}, []     # 'convert'|'check', field, value, expression

  def scan_xls(self):
    for file in self.xls_rules['files']:
      for (dirpath, dirnames, filenames) in walk(self.CONF_PATH+file['path']):
        for entry in scandir(path=dirpath):
          if not entry.is_file() or not entry.name.endswith(SUFFIX):
            continue
          db_row = FileDescr(file['domain'], file['filetype'], entry.name, entry.path, entry.stat().st_size, int(entry.stat().st_mtime))
          key = (db_row.path,db_row.size,db_row.mod_time)
          if key not in self.pg_files:
            self.pg_files[key] = True
            try:
              self.workbook = xlrd.open_workbook(db_row.path)
            except Exception as e:
              logging.error(u'Unable to open the workbook {}'.format(db_row.path))
              logging.error(e)
              continue
            self.sheet  = self.workbook.sheet_by_index(0)
            record, record_errors = self.read_record(file['check'], (0, 0))
            if len(record_errors):
              logging.debug(u'Skip {} file.'.format(db_row.filename))
              continue
#            print(db_row.path)
            query = u"INSERT INTO datawh_files (domain, filetype, filename, path, size, mod_time, create_date) VALUES (%s, %s, %s, %s, %s, %s, now() at time zone 'utc') RETURNING id;"
            logging.info(u'Insert {} into datawh_files table.'.format(db_row.filename))
            logging.debug(query % db_row)
            self.cur.execute(query, db_row)
            self.files_id = self.cur.fetchone()[0]
            self.conn.commit()

            sheet_names = self.workbook.sheet_names()
            for worksheet in file['worksheets']:
              filtered_names = list(filter(lambda x: re.match(worksheet['filter'], x), sheet_names))
              for sheet_name in filtered_names:
                logging.debug(u'Sheet name: {}'.format(sheet_name))
                self.label, self.document, self.errors = '', {}, []     # 'convert'|'check', field, value, expression
                self.sheet = self.workbook.sheet_by_name(sheet_name)
                self.scan_boxes(worksheet['boxes'], (0, 0), worksheet['flow'])
                if 'insert' in worksheet:
                  self.insert_db(worksheet['insert'])

  def scan_boxes(self, boxes, offset, flow):
    pos = offset[:]
    for box in boxes:
      if pos[0] >= self.sheet.nrows or pos[1] >= self.sheet.ncols:
        break
      repeat = box.get('repeat',1)
      box_repeat = 1 if 'record' in box else repeat
      for count in range(box_repeat):
        pos = add_pos(pos, box.get('offset',[0,0]))
        if pos[0] >= self.sheet.nrows or pos[1] >= self.sheet.ncols:
          break
        if 'record' in box:
          new_pos = self.scan_records(box, pos)
          if flow == 'down':
            pos = new_pos
        if 'boxes' in box:
          new_pos = self.scan_boxes(box['boxes'], pos, box['flow'])
          if flow == 'down':
            pos = new_pos
        if 'insert' in box:
          self.insert_db(box['insert'])
    return(pos)

  def scan_records(self, box, pos):
    records = []
    next_pos = box['next_pos']
    repeat = box.get('repeat',1)
    for count in range(repeat):
      if pos[0] >= self.sheet.nrows or pos[1] >= self.sheet.ncols:
        break
      record, record_errors = self.read_record(box['record'], pos)
      try:
        if 'stop' in box and eval(box['stop']):
          break
      except:
        logging.warning(u'Stop exception; record: {}, eval: {}'.format(record, box['stop']))
        break
      pos = add_pos(pos, next_pos)
      if 'filter' in box and not eval(box['filter']):
        continue
      if 'label' in box:
        self.label = eval(box['label'])
      records.append(record)
      self.errors.extend(record_errors)
    if 'key' in box:
      self.document[box['key']] = records
    return(pos)

  def read_record(self, template, offset):
    record = {}
    errors = []     # 'convert'|'check', field, value, expression
    for field, item in template.items():
      row, col, convert, check = item['row']+offset[0], item['col']+offset[1], item.get('convert',''), item.get('check','')
      if row >= self.sheet.nrows or col >= self.sheet.ncols:
        break
      value = self.sheet.cell(row, col).value
      if convert:
        try:
          value = eval(convert)
        except:
          errors.append(('convert_except', field, value, convert))
      if check:
        try:
          if not eval(check):
            errors.append(('check_failed', field, value, check))
        except:
          errors.append(('check_except', field, value, check))
      record[field] = value
    return (record, errors)

  def close(self):
    logging.debug(u'Close db.')
    self.cur.close()
    self.conn.close()

  def run(self):
    logging.info(u'Program start.')
    try:
      rules_mtime = 0
      while True:
        mtime = os.path.getmtime(RULES)
        if rules_mtime != mtime:
          logging.info(u'Reload rules.')
          rules_mtime = mtime
          self.init()
        logging.debug(u'scan_xls start.')
        self.scan_xls()
        logging.debug(u'scan_xls end.')
        if not self.xls_rules['params']['period']:
          logging.info(u'Period is 0.')
          break
        else:
          time.sleep(self.xls_rules['params']['period'])
      self.close()
    except Exception as e:
      logging.critical(e)
    logging.info(u'Program end.')

DataWH().run()
