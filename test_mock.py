#!/usr/bin/env python3
"""模拟测试：假 API 先报两次无容量、再成功；以及配额错误立即停。不连真账号。"""
import types

import yousj_oci_start as m
from oci.exceptions import ServiceError


def svc_error(code, status):
    return ServiceError(status=status, code=code, headers={}, message=f"mock {code}")


class FakeResp:
    def __init__(self, name):
        self.data = types.SimpleNamespace(
            display_name=name, lifecycle_state="PROVISIONING", id="ocid1.mock.instance"
        )


def run():
    job = {"_compartment_id": "ocid1.tenancy.mock", "display_name_prefix": "test"}
    target = {"shape": "VM.Standard.A1.Flex", "ocpus": 2, "memory_gbs": 12}
    ads = ["AD-1", "AD-2", "AD-3"]

    # 用例1：两次 OutOfHostCapacity 后成功 → 应重试并在第3次返回
    calls = {"n": 0}

    def flaky(details):
        calls["n"] += 1
        if calls["n"] < 3:
            raise svc_error("OutOfHostCapacity", 500)
        return FakeResp(details.display_name)

    m.time.sleep = lambda s: None  # 测试里不等
    inst = m.snipe_one(None, job, target, ads, "img", "subnet", "ssh-rsa AAAA",
                       1, interval=1, max_attempts=0, create_fn=flaky)
    assert calls["n"] == 3, calls
    assert inst.id == "ocid1.mock.instance"
    print("用例1 通过：无容量自动重试，第 3 次成功")

    # 用例2：LimitExceeded → 应立即抛错，不重试
    calls2 = {"n": 0}

    def quota(details):
        calls2["n"] += 1
        raise svc_error("LimitExceeded", 400)

    try:
        m.snipe_one(None, job, target, ads, "img", "subnet", "ssh-rsa AAAA",
                    1, interval=1, max_attempts=0, create_fn=quota)
        raise AssertionError("应当抛错")
    except RuntimeError:
        assert calls2["n"] == 1
    print("用例2 通过：配额错误立即停，不瞎重试")

    # 用例3：错误分类
    assert m.classify_error(svc_error("OutOfHostCapacity", 500)) == "retry"
    assert m.classify_error(svc_error("TooManyRequests", 429)) == "retry"
    assert m.classify_error(svc_error("NotAuthenticated", 401)) == "stop"
    assert m.classify_error(svc_error("QuotaExceeded", 400)) == "stop"
    print("用例3 通过：错误分类正确")

    print("全部模拟测试通过 ✅")


if __name__ == "__main__":
    run()
