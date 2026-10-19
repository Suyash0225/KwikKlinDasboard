    join = await client.get("/join")
    assert join.status_code == 200
    assert 'rel="canonical" href="http://test/join"' in join.text
    assert '<meta name="robots" content="index, follow">' in join.text