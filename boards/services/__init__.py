"""
One module per board. Each service composes its board from the factory's
databases and returns a dict the matching fragment template draws.

A service is built with its sources injected, so a test can hand it fakes
and nothing here ever needs the network::

    AdminBoardService("JIVO_OIL", postgres=FakePostgres(), hana_for=hana_factory(FakeHana()))
"""
