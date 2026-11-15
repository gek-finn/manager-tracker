# One low-memory worker. Recheck port 8001 before deployment.
bind = '127.0.0.1:8001'
workers = 1
worker_class = 'sync'
threads = 1
timeout = 30
graceful_timeout = 30
preload_app = False
forwarded_allow_ips = ''
secure_scheme_headers = {}
accesslog = None
errorlog = '-'
capture_output = True
