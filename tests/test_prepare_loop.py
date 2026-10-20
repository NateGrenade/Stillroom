import importlib.util, subprocess, tempfile, unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('prepare_loop',Path(__file__).resolve().parents[1]/'scripts'/'prepare_loop.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
class LoopPreparationTest(unittest.TestCase):
 def test_guarded_loop_timeline_and_no_overwrite(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);source=root/'source.wav';dest=root/'loop.mp3'
   subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=220:duration=20:sample_rate=44100','-ac','2',str(source)],check=True)
   result=module.prepare(source,dest,1,19,2,1)
   self.assertEqual((result['duration'],result['loopStart'],result['loopEnd'],result['decodedSeconds']),(16,1,17,18))
   self.assertLess(result['wrapSampleDelta'],.02)
   with self.assertRaises(ValueError):module.prepare(source,dest,1,19,2,1)
   with self.assertRaises(ValueError):module.prepare(source,root/'invalid.mp3',1,50,2,1)
if __name__=='__main__':unittest.main()
