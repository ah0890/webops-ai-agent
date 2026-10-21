import io
import json

from openpyxl import Workbook
from sqlmodel import select

from app.database.models import Product, Website
from app.services.importer import import_products

CCY = {"USD", "EUR", "GBP"}
CSV = """supplier,product_name,sku,product_url,currency,price_change_threshold,active
Alpha Parts,Bolt M6,B-6,https://alpha.test/bolt-m6,USD,5,yes
Alpha Parts,Bolt M8,B-8,https://alpha.test/bolt-m8,USD,,true
Alpha Parts,Bolt M6 again,B-6b,https://ALPHA.test/bolt-m6/,USD,5,yes
,No supplier,X,https://alpha.test/x,USD,5,yes
Beta Supply,,Y,https://beta.test/y,USD,5,yes
Beta Supply,Bad URL,Z,not-a-url,USD,5,yes
Beta Supply,Bad ccy,Q,https://beta.test/q,ABC,5,yes
Beta Supply,Bad threshold,T,https://beta.test/t,EUR,150,yes
Beta Supply,Nut,N-1,https://beta.test/nut,EUR,2.5%,no
"""


def test_csv_summary_accounts_for_every_row(session):
    s = import_products(session, "p.csv", CSV.encode(), CCY)
    assert s.total_rows == 9
    assert (s.valid, s.imported, s.duplicates, s.invalid) == (3, 3, 1, 5)
    assert s.valid + s.duplicates + s.invalid == s.total_rows
    assert sorted(s.websites_created) == ["Alpha Parts", "Beta Supply"]
    rows = {e["row"] for e in s.errors}
    assert rows == {4, 5, 6, 7, 8, 9}
    nut = session.exec(select(Product).where(Product.sku == "N-1")).one()
    assert nut.active is False and nut.price_change_threshold == 2.5
    alpha = session.exec(select(Website).where(Website.name == "Alpha Parts")).one()
    assert alpha.allowed_domains == ["alpha.test"]


def test_dry_run_writes_nothing(session):
    s = import_products(session, "p.csv", CSV.encode(), CCY, dry_run=True)
    assert s.valid == 3 and s.imported == 0
    assert session.exec(select(Product)).all() == []


def test_reimport_updates(session):
    import_products(session, "p.csv", CSV.encode(), CCY)
    s = import_products(session, "p.csv", CSV.encode(), CCY)
    assert s.imported == 0 and s.updated == 3


def test_existing_supplier_domain_enforced(session):
    session.add(Website(name="Alpha Parts", base_url="https://alpha.test", allowed_domains=["alpha.test"]))
    session.commit()
    s = import_products(session, "p.csv", b"supplier,product_name,product_url\nAlpha Parts,X,https://elsewhere.test/x\n", CCY)
    assert s.invalid == 1 and "outside allowed_domains" in s.errors[0]["errors"][0]


def test_xlsx_and_json(session):
    wb = Workbook()
    ws = wb.active
    ws.append(["Supplier", "Product Name", "SKU", "Product URL", "Currency"])
    ws.append(["Gamma", "Cable", "C1", "https://gamma.test/c1", "GBP"])
    buf = io.BytesIO()
    wb.save(buf)
    assert import_products(session, "p.xlsx", buf.getvalue(), CCY).imported == 1
    data = {"products": [{"supplier": "Gamma", "name": "Plug", "url": "https://gamma.test/p"}]}
    assert import_products(session, "p.json", json.dumps(data).encode(), CCY).imported == 1
