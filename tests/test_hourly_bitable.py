from __future__ import annotations

import json

import httpx
import pytest

from liveops.bitable import BitableClient
from liveops.feishu import FeishuError


async def test_native_hierarchy_and_recovered_record_id_keep_children_under_one_parent():
    fields = [{"field_id":"text","field_name":"主播","type":1},
              {"field_id":"gmv","field_name":"GMV","type":2},
              {"field_id":"key","field_name":"SourceID","type":1}]
    views = [{"view_id":"operator-view","view_name":"原有视图","view_type":"grid"}]
    records, log = {}, []

    async def handler(request):
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        log.append((method,path,body))
        data = {}
        if path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(200,json={"code":0,"tenant_access_token":"token"})
        if path.endswith("/fields"):
            if method=="POST":
                field={"field_id":"parent-link",**body}
                fields.append(field)
                data={"field":field}
            else:
                data={"items":fields}
        elif path.endswith("/views"):
            if method=="POST":
                view={"view_id":"hour-view",**body}
                views.append(view)
                data={"view":view}
            else:
                data={"items":views}
        elif "/views/" in path and method=="PATCH":
            assert path.endswith("hour-view")
            assert body=={"property":{"hierarchy_config":{"field_id":"parent-link"}}}
        elif path.endswith("/records/search"):
            key=body["filter"]["conditions"][0]["value"][0]
            data={"items":[{"record_id":row["record_id"]} for row in records.values()
                            if row["fields"]["SourceID"]==key]}
        elif path.endswith("/records") and method=="POST":
            record={"record_id":f"record-{len(records)+1}",**body}
            records[record["record_id"]]=record
            data={"record":record}
        elif "/records/" in path and method=="PUT":
            record_id=path.rsplit("/",1)[1]
            records[record_id]={"record_id":record_id,**body}
            data={"record":records[record_id]}
        else:
            return httpx.Response(404,json={"code":1,"msg":"unexpected endpoint"})
        return httpx.Response(200,json={"code":0,"data":data})

    client=BitableClient("app","secret","base","table")
    await client._http.aclose()
    client._http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await client.ensure_hour_hierarchy("父记录")
        mapping={"host_name":"主播","gmv":"GMV"}
        parent=await client.upsert_record(values={"host_name":"测试主播","gmv":900},field_mapping=mapping,
            sync_key_field="SourceID",sync_key="shift-1",lookup_sync_key=True)
        child=await client.upsert_record(values={"host_name":"测试主播","gmv":150,"parent":[parent["record_id"]]},
            field_mapping={**mapping,"parent":"父记录"},sync_key_field="SourceID",sync_key="shift-1-hour-2",lookup_sync_key=True)
        # Simulate losing the local response/record ID: search the stable key before retrying creation.
        repeated=await client.upsert_record(values={"host_name":"测试主播","gmv":160,"parent":[parent["record_id"]]},
            field_mapping={**mapping,"parent":"父记录"},sync_key_field="SourceID",sync_key="shift-1-hour-2",lookup_sync_key=True)
        await client.ensure_hour_hierarchy("父记录")
    finally:
        await client.close()
    assert child["record_id"]==repeated["record_id"]
    assert len(records)==2
    assert records[child["record_id"]]["fields"]["父记录"]==[parent["record_id"]]
    assert records[child["record_id"]]["fields"]["GMV"]==160
    assert sum(method=="POST" and path.endswith("/views") for method,path,_ in log)==1
    assert sum(method=="POST" and path.endswith("/fields") for method,path,_ in log)==1


@pytest.mark.parametrize("schema",[{"type":1,"property":{}},{"type":18,"property":{"table_id":"other"}}])
async def test_existing_incompatible_parent_field_is_never_overwritten(schema):
    writes=[]
    async def handler(request):
        if request.url.path.endswith("internal"):
            return httpx.Response(200,json={"tenant_access_token":"token"})
        if request.method!="GET":
            writes.append(request)
        return httpx.Response(200,json={"code":0,"data":{"items":[{
            "field_id":"existing","field_name":"父记录",**schema}]}})
    client=BitableClient("app","secret","base","table")
    await client._http.aclose()
    client._http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(FeishuError,match="不会覆盖"):
            await client.ensure_hour_hierarchy("父记录")
    finally:
        await client.close()
    assert not writes


async def test_fields_are_fully_paginated_before_parent_field_creation():
    queries=[]
    async def handler(request):
        if request.url.path.endswith("internal"):
            return httpx.Response(200,json={"tenant_access_token":"token"})
        queries.append(request.url.params.get("page_token"))
        if request.url.params.get("page_token")=="next":
            return httpx.Response(200,json={"data":{"items":[{"field_name":"父记录"}],"has_more":False}})
        return httpx.Response(200,json={"data":{"items":[{"field_name":"GMV"}],"has_more":True,"page_token":"next"}})
    client=BitableClient("app","secret","base","table")
    await client._http.aclose()
    client._http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert [row["field_name"] for row in await client.list_fields()]==["GMV","父记录"]
    finally:
        await client.close()
    assert queries==[None,"next"]
