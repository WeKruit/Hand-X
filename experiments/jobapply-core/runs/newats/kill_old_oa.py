import psutil,time
now=time.time();k=0
for p in psutil.process_iter(['pid','cmdline','create_time']):
    cl=p.info.get('cmdline') or []
    if any('browser-use-user-data-dir-oa' in a for a in cl) and now-p.info['create_time']>300:
        try:p.kill();k+=1
        except Exception:pass
print(k)
