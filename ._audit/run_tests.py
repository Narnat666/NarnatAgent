import os, sys, unittest

repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "android-ad-cleaner")
os.environ["ADB"] = os.path.join(repo, "fake_adb.bat")
open(os.environ["ADB"], "w").write("@echo off\r\n")
os.chdir(repo)
suite = unittest.defaultTestLoader.discover("tests")
res = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if res.wasSuccessful() else 1)
