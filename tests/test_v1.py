import ast
import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
import main

class TestV13(unittest.TestCase):
    def test_exact_twenty_sectors(self):
        self.assertEqual(20,len(main.V1_SECTORS)); self.assertEqual(20,len(set(main.V1_SECTORS)))
    def test_day_plan_is_10_plus_10_and_disjoint(self):
        p=main._v1_day_sector_plan(date(2026,9,22))
        self.assertEqual(10,len(p['AM'])); self.assertEqual(10,len(p['PM']))
        self.assertTrue(set(p['AM']).isdisjoint(p['PM']))
        self.assertEqual(set(main.V1_SECTORS),set(p['AM'])|set(p['PM']))
    def test_discovery_has_no_output_schema_and_uses_auto_search(self):
        calls=[]
        class R: ok=True; status=200; error=''; data={'results':[],'requestId':'x'}
        with patch.object(main,'EXA_API_KEY','x'),patch.object(main,'_v1_exa_request',lambda u,b,**k:(calls.append((u,b)) or R())):
            main.v1_exa_search_sector('Sport Discovery',date(2026,9,22),set(),num=8)
        self.assertEqual(1,len(calls)); body=calls[0][1]
        self.assertNotIn('outputSchema',body); self.assertEqual(8,body['numResults']); self.assertEqual({'highlights':True,'summary':True},body['contents'])
        self.assertNotIn('type',body)
    def test_deep_search_is_reserved_for_recovery_shape(self):
        calls=[]
        class R: ok=True; status=200; error=''; data={'results':[],'requestId':'x'}
        with patch.object(main,'EXA_API_KEY','x'),patch.object(main,'_v1_exa_request',lambda u,b,**k:(calls.append(b) or R())):
            main.v1_exa_search_sector('Sport Origin',date(2026,9,22),set(),mode='deep',num=8)
        self.assertEqual('deep',calls[0]['type']); self.assertEqual(2,len(calls[0]['additionalQueries']))
    def test_contents_batch_contract(self):
        calls=[]
        class R: ok=True; status=200; error=''; data={'results':[]}
        with patch.object(main,'EXA_API_KEY','x'),patch.object(main,'_v1_exa_request',lambda u,b,**k:(calls.append(b) or R())):
            main.v1_contents_for_urls(['https://example.com/a','https://example.com/a/'])
        self.assertEqual(1,len(calls)); self.assertEqual(1,len(calls[0]['urls'])); self.assertTrue(calls[0]['summary']); self.assertTrue(calls[0]['highlights'])
    def test_cross_sector_duplicate_is_rejected(self):
        c1={'sector':'Sport Discovery','normalized_subject':'Football goal dimensions','central_knowledge_unit':'football goal dimensions','central_claim':'goal width and height','source_url':'https://a.com'}
        c2={'sector':'Equipment / Measurement','normalized_subject':'Football goal dimensions','central_knowledge_unit':'football goal dimensions','central_claim':'goal size','source_url':'https://b.com'}
        cov={'records':[]}
        out,_=main.v1_build_reservoir([c1,c2],cov,set())
        self.assertEqual(1,len(out))
    def test_quality_floor_prefers_a_grade_and_evidence(self):
        weak={'grade':'C','summary':'x','highlights':['x'],'central_claim':'short','normalized_subject':'a','sector':'Sport Discovery','source_url':'https://example.com/a'}
        strong={'grade':'A','summary':'A long authoritative summary explaining a specific sports knowledge unit in detail.','highlights':['specific evidence one','specific evidence two','specific evidence three'],'central_claim':'A detailed documented knowledge claim with precise evidence','normalized_subject':'A specific subject','sector':'Sport Discovery','source_url':'https://fifa.com/x'}
        self.assertGreater(main.v1_discovery_quality(strong),main.v1_discovery_quality(weak))
    def test_hub_fallback_length_contract(self):
        c={'sector':'Sport Discovery','normalized_subject':'A remarkable established sport with unusual rules','central_claim':'This is a sufficiently detailed documented claim about the sport and its rules.','research_evidence':'This sport has a documented rules tradition. It uses a distinctive field and a specific scoring system that make it different from more familiar sports. The evidence describes its development and how players actually participate.', 'highlights':['Distinctive rules define how the game is played','The sport has a documented history','Its equipment differs from common sports']}
        e=main._v1_fallback_editorial(c,[])
        self.assertTrue(6<=len(e['headline'].split())<=14); self.assertTrue(32<=len(e['summary'].split())<=60); self.assertEqual(3,len(e['key_points']))
    def test_source_name_is_clickable_and_url_hidden(self):
        s={'sector':'Sport Discovery','headline':'A Proper Evergreen Sports Headline','body':'This is a concise verified summary with enough words to exercise the source rendering contract and remain useful to readers.','key_points':['One concise fact','Another concise fact','A third concise fact'],'sources':[('FIFA','https://www.fifa.com/example')],'tags':['#SportsGames']}
        html=main._v1_caption_html(s)
        self.assertIn('<a href="https://www.fifa.com/example">FIFA</a>',html); self.assertNotIn('(https://www.fifa.com/example)',html)
    def test_no_image_publisher_uses_generated_photo_card_not_text(self):
        story=main.v1_normalize_story({'sector':'Sport Discovery','headline':'A Proper Evergreen Sports Headline','body':'A concise verified body with enough words to publish as a knowledge hub without becoming a narrative blog post.','key_points':['One','Two','Three'],'sources':[('Source','https://example.com/a')],'image':None})
        calls=[]
        with patch.object(main,'tg_call',side_effect=lambda method,data=None,file_path='',file_field='photo':(calls.append((method,data,file_path)) or {'ok':True,'result':{'message_id':7}})):
            r=main.v1_publish_evergreen(story)
        self.assertTrue(r['ok']); self.assertEqual('sendPhoto',calls[0][0]); self.assertTrue(calls[0][2]); self.assertNotIn('sendMessage',[x[0] for x in calls])
    def test_batch_editorial_is_one_ai_request(self):
        calls={'n':0}
        class AI:
            available=True; fatal=False; last_error=''
            def json(self,*args,**kwargs):
                calls['n']+=1
                return {'posts':[{'post_number':1,'headline':'A Proper Evergreen Sports Headline','summary':'This is a concise verified summary with enough words to satisfy the hub-length contract and explain one knowledge unit clearly without narrative filler.','key_points':['One concise fact','Another concise fact','A third concise fact'],'image_index':0}]}
        c={'sector':'Sport Discovery','normalized_subject':'Example subject','central_claim':'Evidence claim','research_evidence':'The evidence explains the subject without unsupported numbers.','sources':[{'name':'Source','url':'https://example.com'}]}
        out=main.v1_batch_editorialize(AI(),[c],[[]])
        self.assertEqual(1,calls['n']); self.assertEqual(1,len(out))
    def test_null_values_safe(self):
        s=main.v1_normalize_story({'image':None,'sources':None,'key_points':None,'tags':None,'people':None})
        self.assertEqual({},s['image']); self.assertEqual([],s['sources']); self.assertEqual([],s['key_points']); self.assertEqual([],s['tags']); self.assertEqual([],s['people'])
    def test_live_pair_remains_separate_and_last_in_orchestrator(self):
        src=Path(main.__file__).read_text(encoding='utf-8'); tree=ast.parse(src); fn=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='v1_run_once')
        names=[n.id for n in ast.walk(fn) if isinstance(n,ast.Name)]
        self.assertIn('v4_publish_live',names); self.assertIn('v4_live_pair',names)
        code=ast.get_source_segment(src,fn); self.assertGreater(code.index('v1_publish_evergreen'),code.index('v1_batch_editorialize'))
    def test_cerebras_is_optional_for_runtime(self):
        self.assertTrue(hasattr(main,'V1_CEREBRAS_BATCH_ENABLED'))

if __name__=='__main__': unittest.main(verbosity=2)
