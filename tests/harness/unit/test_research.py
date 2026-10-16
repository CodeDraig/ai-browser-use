from pathlib import Path

import pytest
from PIL import Image

from browser_harness import research as r


def test_capture_marks_navigation_incomplete_and_redacts_links(tmp_path, monkeypatch):
    from browser_harness import helpers as h
    tokens = iter([{'token':'a'}, {'token':'b'}])
    monkeypatch.setattr(r, '_document', lambda: next(tokens))
    monkeypatch.setattr(h, 'read_page', lambda **kw: {'url':'https://site/page?token=secret', 'title':'Title','text':'text', 'links':[{'url':'https://site/export?iat=secret&format=jpg'}], 'controls':[], 'document_token':'a'})
    result = r.capture_research(tmp_path/'capture', screenshot=False, metadata={'date':'annotation'})
    assert result['status'] == 'incomplete'
    assert result['annotations']['date'] == 'annotation'
    assert 'secret' not in (tmp_path/'capture/page.json').read_text()
    assert result['source']['url'] == 'https://site/page'


def test_save_research_never_exports_and_checks_hash(tmp_path, monkeypatch):
    from browser_harness import helpers as h
    def save(download, dest):
        p=Path(dest)/'scan.txt';p.write_text('source')
        return {'path':str(p),'sha256':r._hash(p)}
    monkeypatch.setattr(h,'save_download',save)
    result=r.save_research_download('profile:1',tmp_path/'saved',source={'url':'https://site/page','title':'Paper'},metadata={'note':'annotation'})
    assert result['status']=='completed'
    assert result['source_provenance']=='caller_supplied'
    assert result['artifacts'][0]['sha256']==r._hash(tmp_path/'saved/original/scan.txt')


def image_source(tmp_path, frames=1):
    p=tmp_path/'original.tiff'
    images=[Image.new('RGB',(100,100),'white') for _ in range(frames)]
    images[0].save(p,save_all=True,append_images=images[1:])
    return p


def fake_tools(monkeypatch):
    class Result:
        returncode=0;stderr=b''
        def __init__(self,stdout):self.stdout=stdout
    monkeypatch.setattr(r.shutil,'which',lambda tool:tool)
    def run(args,**kw):
        if '--version' in args or '-v' in args:return Result(b'Test tool 1\n')
        if args[0]=='pdfinfo':return Result(b'Pages: 2\n')
        if args[0]=='pdftotext':return Result(b'Embedded text' if args[args.index('-f')+1]=='1' else b'')
        if args[0]=='pdftoppm':Image.new('RGB',(10,10)).save(args[-1]+'.png');return Result(b'')
        return Result(b'OCR text')
    monkeypatch.setattr(r.subprocess,'run',run)


def test_page_limit_resume_preserves_original_and_verified_outputs(tmp_path,monkeypatch):
    p=image_source(tmp_path,2);digest=r._hash(p);fake_tools(monkeypatch)
    out=tmp_path/'extracted'
    first=r.extract_document(p,out,max_pages=1)
    assert first['status']=='incomplete' and 'page_limit' in first['error']
    second=r.extract_document(p,out,max_pages=1)
    assert second['status']=='completed' and len(second['pages'])==2
    assert r._hash(p)==digest
    assert all(x['uncorrected'] for x in second['pages'].values())
    with pytest.raises(ValueError):r.extract_document(p,out,languages='fra')


def test_missing_tool_returns_actionable_incomplete_manifest(tmp_path,monkeypatch):
    p=image_source(tmp_path);monkeypatch.setattr(r.shutil,'which',lambda name:None)
    result=r.extract_document(p,tmp_path/'out')
    assert result['status']=='incomplete' and 'Install Tesseract' in result['error']


def test_mixed_pdf_uses_text_and_ocr_separately(tmp_path,monkeypatch):
    p=tmp_path/'source.pdf';p.write_bytes(b'%PDF-test');fake_tools(monkeypatch)
    result=r.extract_document(p,tmp_path/'out')
    assert result['status']=='completed'
    assert result['pages']['1']['engine']=='embedded_pdf_text'
    assert result['pages']['2']['engine']=='tesseract_ocr'


def test_timeout_retains_page_results(tmp_path,monkeypatch):
    p=image_source(tmp_path,2);fake_tools(monkeypatch)
    original=r.subprocess.run;calls=0
    def fail(args,**kw):
        nonlocal calls
        if args[0]=='tesseract' and 'stdout' in args:
            calls+=1
            if calls==2:raise r.subprocess.TimeoutExpired(args,1)
        return original(args,**kw)
    monkeypatch.setattr(r.subprocess,'run',fail)
    result=r.extract_document(p,tmp_path/'out')
    assert result['status']=='incomplete'
    assert result['pages']['1']['status']=='completed'
    assert result['pages']['2']['status']=='failed'


def test_real_local_mixed_pdf_and_image_extraction(tmp_path):
    """Small actual PDF with embedded text on page 1 and raster text on page 2."""
    import io

    from PIL import ImageDraw, ImageFont
    if any(not r.shutil.which(t) for t in ('pdfinfo','pdftotext','pdftoppm','tesseract')):
        pytest.skip('optional local OCR/PDF tools unavailable')
    image=Image.new('RGB',(800,200),'white')
    ImageDraw.Draw(image).text((30,60),'Research scan text',fill='black',font=ImageFont.truetype('/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf',36) if Path('/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf').exists() else ImageFont.load_default(size=36))
    raw=io.BytesIO();image.save(raw,format='JPEG');jpeg=raw.getvalue()
    def stream(data,attributes=b''):
        return b'<< /Length '+str(len(data)).encode()+b' '+attributes+b' >>\nstream\n'+data+b'\nendstream'
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 300] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 300] /Resources << /XObject << /Im1 8 0 R >> >> /Contents 7 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',stream(b'BT /F1 24 Tf 50 200 Td (Embedded research text) Tj ET'),
        stream(b'q 600 0 0 150 0 80 cm /Im1 Do Q'),stream(jpeg,b'/Type /XObject /Subtype /Image /Width 800 /Height 200 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode')]
    data=bytearray(b'%PDF-1.4\n');offsets=[0]
    for i,obj in enumerate(objects,1):offsets.append(len(data));data.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    xref=len(data);data.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode())
    source=tmp_path/'mixed.pdf';source.write_bytes(data)
    result=r.extract_document(source,tmp_path/'pdf-text',timeout=30)
    assert result['status']=='completed',result
    assert result['pages']['1']['engine']=='embedded_pdf_text'
    assert result['pages']['2']['engine']=='tesseract_ocr'
    assert 'Embedded research text' in (tmp_path/'pdf-text/page-0001.txt').read_text()
    assert 'Research' in (tmp_path/'pdf-text/page-0002.txt').read_text()
    source_image=tmp_path/'image.jpg';source_image.write_bytes(jpeg)
    result=r.extract_document(source_image,tmp_path/'image-text',timeout=30)
    assert result['status']=='completed',result
    assert 'Research' in (tmp_path/'image-text/page-0001.txt').read_text()


def test_original_manifest_filename_is_not_overwritten(tmp_path,monkeypatch):
    from browser_harness import helpers as h
    def save(download,dest):
        p=Path(dest)/'manifest.json';p.write_text('original newspaper export')
        return {'path':str(p),'sha256':r._hash(p)}
    monkeypatch.setattr(h,'save_download',save)
    result=r.save_research_download('one:1',tmp_path/'saved',source='https://site/page')
    assert result['artifacts'][0]['path']=='original/manifest.json'
    assert (tmp_path/'saved/original/manifest.json').read_text()=='original newspaper export'
