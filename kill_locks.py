import pymysql
conn = pymysql.connect(host='34.27.149.229', port=3306, user='sales_user', password='sales@123', database='sales', autocommit=True)
cur = conn.cursor()
cur.execute('SHOW PROCESSLIST')
rows = cur.fetchall()
print(f'{len(rows)} connections:')
for r in rows:
    q = str(r[7])[:80] if r[7] else 'N/A'
    print(f'  pid={r[0]} cmd={r[4]} time={r[5]}s state={r[6]} query={q}')
conn.close()
