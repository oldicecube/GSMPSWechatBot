"""Offline queue ordering checks; no WeChat UI or HTTP sends."""
import threading
import time
import unittest
from core.wechat_sender.send_queue import PrioritySendQueue

class BulkPriorityTests(unittest.TestCase):
    def wait(self, predicate):
        deadline=time.monotonic()+3
        while not predicate():
            if time.monotonic()>deadline:self.fail("queue condition timed out")
            time.sleep(0.005)

    def test_all_non_bulk_sources_overtake_waiting_bulk_even_with_huge_priority(self):
        queue=PrioritySendQueue(idle_grace_seconds=0)
        entered,release=threading.Event(),threading.Event()
        order=[];threads=[]
        def hold():
            entered.set();self.assertTrue(release.wait(3));order.append("active")
        first=threading.Thread(target=lambda:queue.submit(hold,source="bot"));first.start()
        self.assertTrue(entered.wait(3))
        jobs=[("bulk1","print-bulk-image",-10000),("bulk2","PRINT-BULK-IMAGE",0),("external","other-app",10**300),("bot","bot",100),("print","print",0)]
        try:
            for count,(label,source,priority) in enumerate(jobs,1):
                thread=threading.Thread(target=lambda label=label,source=source,priority=priority:queue.submit(lambda:order.append(label),source=source,priority=priority))
                thread.start();threads.append(thread);self.wait(lambda:queue.pending==count)
            status=queue.snapshot()
            self.assertTrue(status["bulk_is_lowest"])
            self.assertEqual(status["pending_bulk"],2)
            self.assertEqual(status["pending_non_bulk"],3)
            self.assertEqual(status["active"],1)
            release.set();first.join(3)
            for thread in threads:thread.join(3)
            self.assertEqual(order,["active","print","bot","external","bulk1","bulk2"])
        finally:
            release.set();queue.stop(drain=True)

    def test_bulk_rank_does_not_disable_normal_default_or_direct_jobs(self):
        queue=PrioritySendQueue()
        try:
            self.assertEqual(queue.submit(lambda:"normal",priority=float("inf"),source="external"),"normal")
            self.assertEqual(queue.submit(lambda:"bulk",priority=0,source=queue.BULK_SOURCE),"bulk")
            self.assertEqual(queue.snapshot()["policy_version"],"bulk-last-v1")
            self.assertFalse(queue.snapshot()["ui_operation_preemptible"])
        finally:queue.stop(drain=True)

if __name__=="__main__":unittest.main()
