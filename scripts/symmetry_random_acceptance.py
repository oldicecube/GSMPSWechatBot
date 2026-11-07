"""Random-axis symmetry regression, using synthetic media and fake senders."""
from io import BytesIO
import math
from pathlib import Path
import random
import unittest
from unittest.mock import Mock,patch

from PIL import Image
import symmetry_animation_acceptance as fixtures

codec,picture,followup=fixtures.codec,fixtures.picture,fixtures.followup


class RandomSymmetry(unittest.TestCase):
    def test_command_default_count_bounds_and_legacy_directions(self):
        self.assertEqual(picture._parse_symmetry_command('/对称 随机'),{'mode':'random','iterations':1})
        for count in (1,5,50):
            self.assertEqual(picture._parse_symmetry_command(f'/对称 随机 {count}'),{'mode':'random','iterations':count})
        for value in ('0','-1','51','1000000000000000000000','1.5','2 3','abc','+2'):
            self.assertIn('error',picture._parse_symmetry_command('/对称 随机 '+value))
        for label,mode in zip(('左右','右左','上下','下上'),fixtures.MODES):
            self.assertEqual(picture._parse_symmetry_command('/对称 '+label),mode)
        self.assertIsNone(picture._parse_symmetry_command('/对称其他 随机 3'))

    def test_axes_randomize_both_position_and_angle_reproducibly(self):
        axes=codec.random_axes((40,30),10,random.Random(42))
        self.assertEqual(axes,codec.random_axes((40,30),10,random.Random(42)))
        self.assertEqual(len(set(axes)),10)
        self.assertGreater(len({axis.angle for axis in axes}),1)
        self.assertGreater(len({(axis.x,axis.y) for axis in axes}),1)
        for axis in axes:
            self.assertTrue(0<=axis.angle<=math.pi and 0<=axis.x<=40 and 0<=axis.y<=30)

    def test_axis_geometry_matches_four_legacy_symmetries_and_alpha(self):
        image=Image.new('RGBA',(7,5))
        for y in range(5):
            for x in range(7):image.putpixel((x,y),(x*20,y*30,10,128 if x%2 else 255))
        axes=(codec.SymmetryAxis(math.pi/2,3.5,2.5,True),codec.SymmetryAxis(math.pi/2,3.5,2.5,False),
              codec.SymmetryAxis(0,3.5,2.5,False),codec.SymmetryAxis(0,3.5,2.5,True))
        for axis,mode in zip(axes,fixtures.MODES):
            actual=codec.reflect_axis(image,axis);expected=codec.symmetry_frame(image,mode)
            self.assertEqual(actual.tobytes(),expected.tobytes(),mode)

    def test_diagonal_reflects_coordinates_instead_of_rotating_canvas(self):
        image=Image.new('RGBA',(7,7))
        for y in range(7):
            for x in range(7):image.putpixel((x,y),(x*20,y*30,90,128))
        out=codec.reflect_axis(image,codec.SymmetryAxis(math.pi/4,0,0,True))
        self.assertEqual(out.size,image.size)
        for y in range(7):
            for x in range(7):
                self.assertEqual(out.getpixel((x,y)),image.getpixel((x,y) if y>=x else (y,x)))

    def test_offcentre_reflection_fills_outside_with_transparency(self):
        image=Image.new('RGBA',(7,5),(0,0,0,255))
        out=codec.reflect_axis(image,codec.SymmetryAxis(math.pi/2,2,0,True))
        self.assertEqual(out.getpixel((3,2)),(0,0,0,255))
        self.assertEqual(out.getpixel((6,2))[3],0)

    def test_passes_are_sequential_on_previous_result(self):
        image=Image.new('RGBA',(9,7))
        for y in range(7):
            for x in range(9):image.putpixel((x,y),(x*20,y*30,90,255))
        axes=(codec.SymmetryAxis(math.pi/2,3,1,True),codec.SymmetryAxis(math.pi/4,4,3,False))
        expected=codec.reflect_axis(codec.reflect_axis(image,axes[0]),axes[1])
        self.assertEqual(codec.random_symmetry_frame(image,axes).tobytes(),expected.tobytes())
        self.assertNotEqual(codec.random_symmetry_frame(image,tuple(reversed(axes))).tobytes(),expected.tobytes())

    def test_arbitrary_axis_pixels_follow_exact_halfplane_and_reflection(self):
        image=Image.new('RGBA',(13,11))
        for y in range(11):
            for x in range(13):image.putpixel((x,y),(x*15,y*20,40,128))
        for axis in codec.random_axes(image.size,10,random.Random(99)):
            nx,ny=-math.sin(axis.angle),math.cos(axis.angle)
            offset=nx*axis.x+ny*axis.y
            out=codec.reflect_axis(image,axis)
            for y in range(11):
                for x in range(13):
                    distance=nx*(x+.5)+ny*(y+.5)-offset
                    if (distance>=0)==axis.keep_positive:
                        expected=image.getpixel((x,y))
                    else:
                        sx=math.floor(x+.5-2*distance*nx);sy=math.floor(y+.5-2*distance*ny)
                        expected=image.getpixel((sx,sy)) if 0<=sx<13 and 0<=sy<11 else (0,0,0,0)
                    self.assertEqual(out.getpixel((x,y)),expected,(axis,x,y))

    def test_gif_uses_one_axis_plan_for_all_frames_preserving_timing_and_alpha(self):
        data=fixtures.animated()
        axes=(codec.SymmetryAxis(math.pi/2,4.5,3.5,True),)*3
        with patch.object(codec,'random_axes',return_value=axes) as plan:
            path=codec.save_symmetry(data,'random',fixtures.INSTANCE.name,iterations=3)
        plan.assert_called_once_with((9,7),3,None)
        with Image.open(BytesIO(data)) as source,Image.open(path) as out:
            self.assertEqual(out.n_frames,source.n_frames);self.assertEqual(out.info['loop'],2)
            for index in range(source.n_frames):
                source.seek(index);out.seek(index)
                self.assertEqual(out.info['duration'],source.info['duration'])
                expected=codec.random_symmetry_frame(source,axes);actual=out.convert('RGBA')
                for xy in ((x,y) for x in range(9) for y in range(7)):
                    a,b=actual.getpixel(xy),expected.getpixel(xy)
                    self.assertEqual(a[3],b[3])
                    if b[3]:self.assertEqual(a,b)

    def test_command_payload_reaches_static_and_animated_processing_on_both_platforms(self):
        image=Image.new('RGBA',(9,7),(20,30,40,128));buf=BytesIO();image.save(buf,'PNG')
        for platform in ('wechat','qq'):
            for data,suffix in ((buf.getvalue(),'.png'),(fixtures.animated(),'.gif')):
                context={'platform':platform,'sessionId':'random-'+platform,'group':'random-'+platform,
                         'wxid':'fixture-user','content':''}
                received=[]
                def send(**kwargs):
                    path=Path(kwargs['file_path']);self.assertEqual(path.suffix,suffix)
                    self.assertTrue(path.is_file());received.append(path);return True,''
                with patch.object(picture,'settings',return_value={'followup_ttl_seconds':60}), \
                     patch.object(picture,'SAVE_DIR',fixtures.INSTANCE.name),patch.object(picture,'send',side_effect=send), \
                     patch.object(picture,'save_symmetry',wraps=codec.save_symmetry) as save:
                    picture.handle_auto({**context,'command':'/对称','args':'随机 3'})
                    self.assertEqual(followup.peek(context['sessionId'],wxid=context['wxid'])['payload'],{'mode':'random','iterations':3})
                    picture.handle_auto({**context,'is_emoji':True,'_image_binary':data})
                    self.assertEqual(len(received),1)
                    self.assertEqual(save.call_args.kwargs['iterations'],3)
                    self.assertFalse(received[0].exists())

    def test_invalid_parameters_do_not_arm_or_consume_followup(self):
        context={'sessionId':'random-invalid','wxid':'fixture','command':'/对称','args':'随机 -1'}
        with patch.object(picture,'register_followup') as register:
            self.assertIn('用法',picture.handle_auto(context));register.assert_not_called()
            self.assertFalse(picture.allow_llm(context))

    def test_iteration_weighted_resource_budget_rejects_instead_of_truncating(self):
        before=set(Path(fixtures.INSTANCE.name).glob('symmetry_*'))
        with patch.object(codec,'MAX_TRANSFORM_PIXELS',1000),self.assertRaises(ValueError):
            codec.save_symmetry(fixtures.animated(),'random',fixtures.INSTANCE.name,iterations=10)
        self.assertEqual(before,set(Path(fixtures.INSTANCE.name).glob('symmetry_*')))
        for count in (0,-1,51,1.5,True):
            with self.assertRaises(ValueError):codec.random_axes((9,7),count)

if __name__=='__main__':unittest.main(verbosity=2)
