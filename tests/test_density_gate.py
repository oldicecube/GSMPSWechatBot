"""Offline clock-driven proactive request scheduling; no APIs or Bot start."""
import unittest
from unittest.mock import patch
from llm.density_gate import DensityGate
from llm.proactive_reply import ProactiveReplyManager
from llm.conversation_pulse import ConversationPulse
from core.dispatcher import Dispatcher
from types import SimpleNamespace

class GateTests(unittest.TestCase):
    def batch(self,n,now):return [{'timestamp':now,'content':'消息','message_id':str(i)} for i in range(n)]
    def test_thresholds_and_low_density_accumulation(self):
        gate=DensityGate()
        for now in [1000,1005,1020,1100]:
            self.assertFalse(gate.admit('low',now,1,self.batch(1,now))['admit'])
        decision=gate.admit('low',1200,1,self.batch(4,1200));self.assertTrue(decision['admit']);self.assertEqual(decision['stage'],'low')
        self.assertFalse(gate.admit('low',1400,1,self.batch(4,1400))['admit'])
        self.assertTrue(gate.admit('medium',1200,4,self.batch(3,1200))['admit'])
        self.assertTrue(gate.admit('high',1200,8,self.batch(2,1200))['admit'])
    def test_window_interval_budget_and_no_keepalive(self):
        gate=DensityGate();self.assertTrue(gate.admit('g',1000,10,self.batch(2,1000))['admit'])
        self.assertFalse(gate.admit('g',1001,10,self.batch(2,1001))['admit'])
        self.assertFalse(gate.admit('g',1020,10,[])['admit'])
        for now in [1020,1040,1060]:self.assertTrue(gate.admit('g',now,10,self.batch(2,now))['admit'])
        self.assertEqual(gate.admit('g',1080,10,self.batch(2,1080))['reason'],'window_budget')
        self.assertFalse(gate.admit('g',1100,10,self.batch(10,1100))['admit'])
    def test_quiet_gap_ends_burst_instead_of_cold_one_message_call(self):
        gate=DensityGate();self.assertTrue(gate.admit('g',1000,8,self.batch(2,1000))['admit'])
        result=gate.admit('g',1090,1,self.batch(2,1090))
        self.assertFalse(result['admit']);self.assertEqual(result['reason'],'window_cooldown')
    def test_window_does_not_extend_on_each_new_message(self):
        gate=DensityGate()
        first=gate.admit('g',1000,10,self.batch(2,1000))
        second=gate.admit('g',1020,10,self.batch(2,1020))
        self.assertEqual(first['window_end'],1120)
        self.assertEqual(second['window_end'],1120)
        gate.admit('g',1040,10,self.batch(2,1040))
        gate.admit('g',1060,10,self.batch(2,1060))
        self.assertFalse(gate.admit('g',1120,10,self.batch(20,1120))['admit'])

    def test_group_isolation_and_config_bounds(self):
        gate=DensityGate({'density_burst_max_requests':float('nan')})
        self.assertTrue(gate.admit('a',1000,8,self.batch(2,1000))['admit'])
        self.assertTrue(gate.admit('b',1000,8,self.batch(2,1000))['admit'])
        self.assertEqual(gate.number('density_burst_max_requests'),4)

class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.now=1000
        self.manager=ProactiveReplyManager({'prefix_mode':'mixed','auto_reply':{'enabled':False},'target_groups':['g']})
        self.manager.enabled=True
        self.manager.clock=lambda:self.now
        self.manager.callback=lambda *args:None
    def tearDown(self):self.manager.shutdown()
    def msg(self,i,text='普通讨论文字',**kw):
        return dict(group='g',is_group=True,user='Member',wxid='wxid_test_member',sessionId='g',content=text,raw={'messageKey':str(i),'_ts':self.now},**kw)
    def take(self):
        state=self.manager.states['g']
        with self.manager.lock:
            self.manager._update_density_locked('g',state,self.now)
            return self.manager._take_pending_batch_locked(state,'g')
    def test_sparse_long_messages_do_not_promote_every_message_to_working(self):
        self.manager.handle_message(self.msg(1))
        self.assertIsNone(self.take());self.assertEqual(len(self.manager.states['g']['pending_batch']),1)
        self.now+=200;self.manager.handle_message(self.msg(2))
        self.assertIsNone(self.take());self.assertEqual(len(self.manager.states['g']['pending_batch']),2)
    def test_accumulated_context_flushes_once_and_does_not_replay_empty_batch(self):
        for i in range(4):
            self.now=1000+i*100;self.manager.handle_message(self.msg(i))
        result=self.take();self.assertIsNotNone(result)
        self.assertEqual(len(result[1]),4);self.assertTrue(result[0]['_density_admitted'])
        state=self.manager.states['g'];state['batch_flushing']=False
        self.assertIsNone(self.take())
    def test_explicit_target_bypasses_gate_without_discarding_pending_context(self):
        self.manager.handle_message(self.msg(1))
        self.now+=1;self.manager.handle_message(self.msg(2,directed_to_bot=True))
        result=self.take();self.assertIsNotNone(result);self.assertEqual(len(result[1]),2)
        self.assertTrue(result[2])
    def test_prefix_not_batched_and_other_member_messages_do_not_raise_density(self):
        self.manager.handle_message(self.msg(1,directed_to_other=True))
        self.assertEqual(self.manager.states['g']['density_rate'],0)
        self.assertEqual(self.manager.states['g']['pending_batch'],[])
        self.manager.handle_message(self.msg(2,prefix_used=True))
        self.assertEqual(self.manager.states['g']['pending_batch'],[])
    def test_expired_pending_context_never_triggers_model(self):
        self.manager.handle_message(self.msg(1));self.now+=601
        self.assertIsNone(self.take());self.assertEqual(self.manager.states['g']['pending_batch'],[])
    def test_fifty_sparse_messages_produce_bounded_windows(self):
        admissions=[]
        for i in range(50):
            self.now=1000+i*120;self.manager.handle_message(self.msg(i))
            result=self.take()
            if result:
                admissions.append(self.now);self.manager.states['g']['batch_flushing']=False
        self.assertLessEqual(len(admissions),10)
        self.assertGreater(len(admissions),0)
    def test_timer_dispatches_admitted_batch_without_periodic_idle_requests(self):
        for i in range(4):
            self.now=1000+i*100;self.manager.handle_message(self.msg(i))
        self.now+=20
        submitted=[]
        def quit(_):self.manager.enabled=False
        with patch.object(self.manager,'_submit_task',side_effect=lambda target,args:submitted.append((target,args)) or True),patch('llm.proactive_reply.time.sleep',side_effect=quit):
            self.manager._timer_loop()
        self.assertEqual(len(submitted),1)
        self.assertEqual(submitted[0][0],self.manager._run_callback)
        context=submitted[0][1][1]
        self.assertTrue(context['_density_admitted'])
        self.assertEqual(len(submitted[0][1][2]),4)
        self.manager.states['g']['batch_flushing']=False
        self.manager.enabled=True;self.now+=180
        submitted.clear()
        with patch.object(self.manager,'_submit_task',side_effect=lambda target,args:submitted.append((target,args)) or True),patch('llm.proactive_reply.time.sleep',side_effect=quit):
            self.manager._timer_loop()
        self.assertEqual(submitted,[])

    def test_direct_cycle_does_not_extend_ordinary_request_window(self):
        for i in range(4):
            self.now=1000+i*100;self.manager.handle_message(self.msg(i))
        self.take()
        before=self.manager.states['g']['phase_end']
        self.now+=60
        self.manager.on_llm_result({'group':'g'},{'_llm_ok':True,'should_reply':True,'messages':['reply']})
        self.assertEqual(self.manager.states['g']['phase_end'],before)

    def test_dispatcher_passes_gate_admission_to_service(self):
        d=Dispatcher();calls=[]
        d.llm_service=SimpleNamespace(handle_batch_message=lambda **kw:calls.append(kw) or {'messages':[],'should_reply':False})
        d._dispatch_llm_unlimited({'group':'g','_density_admitted':True},batch_messages=[])
        self.assertTrue(calls[0]['density_admitted'])
    def test_pulse_cooldown_applies_to_substantive_ordinary_messages(self):
        pulse=ConversationPulse({'pulse_plan_cooldown_seconds':35});pulse.clock=lambda:1000
        batch=[{'content':'这是一段需要讨论的普通文字内容','timestamp':1000}]
        self.assertEqual(pulse.decide('g',batch,batch)[0],'plan')
        pulse.clock=lambda:1005
        self.assertEqual(pulse.decide('g',batch,batch)[0],'defer')
        self.assertEqual(pulse.decide('g',[dict(batch[0],prefix_used=True)],batch)[0],'plan')

if __name__=='__main__':unittest.main()
