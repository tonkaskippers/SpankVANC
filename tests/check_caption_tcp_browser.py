"""Isolated caption TCP form check; never starts a route or opens sockets."""
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parent.parent
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from spankvanc.engine import Engine
from spankvanc.server import make_handler
from test_core import route
from test_caption_tcp import STOCK

shot=sys.argv[1] if len(sys.argv)>1 else None
with tempfile.TemporaryDirectory() as folder,patch('spankvanc.engine.doctor',return_value=STOCK):
    engine=Engine(Path(folder));engine.save(route())
    server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(engine,'tcp-test',0))
    port=server.server_port;server.RequestHandlerClass=make_handler(engine,'tcp-test',port)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1300,'height':1400});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{port}')
            card=page.locator('.output').first
            card.locator('[data-field="kind"]').select_option('tcp')
            expect(card.locator('[data-field="captions"]')).to_have_count(0)
            card.locator('[data-field="address"]').fill('tcp://192.0.2.10:2001')
            card.locator('[data-field="tcp_line_ending"]').select_option('cr')
            card.locator('[data-field="tcp_uppercase"]').check()
            page.locator('#save').click()
            expect(page.locator('#notice')).to_have_text('Route saved locally.')
            out=engine.config['outputs'][0]
            assert (out['kind'],out['captions'],out['scte'],out['video_codec'],out['tcp_line_ending'],out['tcp_uppercase'])==('tcp','recaption','strip','text','cr',True),out
            page.locator('#validate').click()
            expect(page.locator('#plan-outputs')).to_contain_text('Raw caption text / TCP')
            page.reload()
            expect(card.locator('[data-field="address"]')).to_have_value('tcp://192.0.2.10:2001')
            expect(card.locator('[data-field="tcp_uppercase"]')).to_be_checked()
            card.locator('[data-field="kind"]').select_option('udp')
            expect(card.locator('[data-field="captions"]')).to_have_value('pass')
            if shot:
                card.locator('[data-field="kind"]').select_option('tcp');card.locator('[data-field="address"]').fill('tcp://192.0.2.10:2001')
                card.screenshot(path=shot)
            assert not errors,errors
            browser.close()
    finally:
        server.shutdown()
print('caption TCP browser check passed')
