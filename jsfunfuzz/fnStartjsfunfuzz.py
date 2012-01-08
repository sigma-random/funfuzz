#!/usr/bin/env python
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import platform
import shutil
import subprocess
import sys
import shlex
import time

from multiprocessing import cpu_count

verbose = True  # Turn this to True to enable verbose output for debugging.
showCapturedCommands = False

# This is used for propagating the global repository directory across functions in this file.
globalRepo = ''

def macType():
    # Script has only been tested on Snow Leopard and Lion.
    assert 6 <= int(platform.mac_ver()[0].split('.')[1]) <= 7
    isSL = isMac and platform.mac_ver()[0].split('.')[1] == '6' \
        and platform.mac_ver()[0].split('.') >= ['10', '6']
    isLion = isMac and platform.mac_ver()[0].split('.')[1] == '7' \
        and platform.mac_ver()[0].split('.') >= ['10', '7']
    return (isSL, isLion)

isMac = False
if platform.system() == 'Darwin':
    isMac = True
    (isSnowLeopard, isLion) = macType()

def verboseDump(inp, verbose=True):
    '''
    This function appends the word 'DEBUG' to any verbose output.
    '''
    if verbose:
        print 'DEBUG -', inp

def captureStdout(cmd, ignoreStderr=False, combineStderr=False, ignoreExitCode=False, currWorkingDir=os.getcwdu()):
    '''
    This function captures standard output into a python string.
    '''
    if showCapturedCommands:
        print ' '.join(cmd)
    p = subprocess.Popen(cmd,
        stdin = subprocess.PIPE,
        stdout = subprocess.PIPE,
        stderr = subprocess.STDOUT if combineStderr else subprocess.PIPE,
        cwd=currWorkingDir)
    (stdout, stderr) = p.communicate()
    if not ignoreExitCode and p.returncode != 0:
        # Potential problem area: Note that having a non-zero exit code does not mean that the operation
        # did not succeed, for example when compiling a shell. A non-zero exit code can appear even
        # though a shell compiled successfully. This issue has been bypassed in the makeShell
        # function in autoBisect.
        # Pymake in builds earlier than revision 232553f741a0 did not support the '-s' option.
        if 'no such option: -s' not in stdout:
            print 'Nonzero exit code from ' + repr(cmd)
            print stdout
        if stderr is not None:
            print stderr
        # Pymake in builds earlier than revision 232553f741a0 did not support the '-s' option.
        if 'no such option: -s' not in stdout:
            raise Exception('Nonzero exit code')
    if not combineStderr and not ignoreStderr and len(stderr) > 0:
        print 'Unexpected output on stderr from ' + repr(cmd)
        print stdout, stderr
        raise Exception('Unexpected output on stderr')
    if showCapturedCommands:
        print stdout
        if stderr is not None:
            print stderr
    return stdout.rstrip()

def hgHashAddToFuzzPath(fuzzPath, currWorkingDir=os.getcwdu()):
    '''
    This function finds the mercurial revision and appends it to the directory name.
    It also prompts if the user wants to continue, should the repository not be on tip.
    '''
    verboseDump('About to start running `hg identify -i -n -b` ...')
    hgIdFull = captureStdout(['hg', 'identify', '-i', '-n', '-b'], currWorkingDir=os.getcwdu())
    hgIdChangesetHash = hgIdFull.split(' ')[0]
    hgIdLocalNum = hgIdFull.split(' ')[1]
    hgIdBranch = hgIdFull.split(' ')[2]  # If on tip, value should be 'default'.
    onDefaultTip = True
    if hgIdBranch != 'default':
        print 'The repository is at this changeset -', hgIdLocalNum + ':' + hgIdChangesetHash
        notOnDefaultTipApproval = str(raw_input('Not on default tip! Are you sure you want to continue? (y/n): '))
        if notOnDefaultTipApproval == ('y' or 'yes'):
            onDefaultTip = False
        else:
            switchToDefaultTipApproval = str(raw_input('Do you want to switch to the default tip? (y/n): '))
            if switchToDefaultTipApproval == ('y' or 'yes'):
                subprocess.call(['hg', 'up', 'default'])
            else:
                raise Exception('Not on default tip.')
    fuzzPath = fuzzPath[:-1] + '-' + hgIdLocalNum + '-' + hgIdChangesetHash + os.sep
    verboseDump('Finished running `hg identify -i -n -b`.')
    return os.path.normpath(fuzzPath), onDefaultTip

def cpJsTreeDir(repo, dest, sourceDir):
    '''
    This function copies the js tree or the pymake build directory.
    '''
    # This globalRepo variable is only needed to propagate the repository to compileCopy function, it can be
    # removed if compileCopy accepts a repo directory as one of its parameters.
    global globalRepo
    globalRepo = repo
    if sourceDir == 'jsSrcDir':
        repo = os.path.normpath(os.path.join(repo, 'js', 'src'))
    # Changeset 91a8d742c509 introduced a mfbt directory on the same level as the js directory.
    elif sourceDir == 'mfbtDir':
        repo = os.path.normpath(os.path.join(repo, 'mfbt'))
    # Changeset b9c673621e1e introduced a public directory on the same level as the js/src directory.
    elif sourceDir == 'jsPublicDir':
        repo = os.path.normpath(os.path.join(repo, 'js', 'public'))
    else:
        raise Exception('Unknown sourceDir:', sourceDir)
    if 'Windows-XP' not in platform.platform():
        repo = os.path.expanduser(repo)
    assert os.path.isdir(repo)
    try:
        verboseDump('Copying the js tree, which is located at ' + repo)
        shutil.copytree(os.path.normpath(repo), dest, ignore=shutil.ignore_patterns('tests', 'trace-test', 'xpconnect'))
        verboseDump('Finished copying the js tree')
    except OSError as e:
        if verbose:
            print repr(e)
        raise Exception("Either the js tree directory located at '" + repo + "' doesn't exist, or the destination already exists.")
    except:
        raise Exception("Problem copying files related to the js tree.")

def autoconfRun(cwd):
    '''
    Sniff platform and run different autoconf types:
    '''
    if platform.system() == 'Darwin':
        subprocess.check_call(['autoconf213'], cwd=cwd)
    elif platform.system() == 'Linux':
        subprocess.check_call(['autoconf2.13'], cwd=cwd)
    elif platform.system() == 'Windows':
        subprocess.check_call(['sh', 'autoconf-2.13'], cwd=cwd)

def cfgJsBin(archNum, compileType, threadsafe, configure, objdir):
    '''
    This function configures a js binary depending on the parameters.
    '''
    cfgCmdList = []
    cfgEnvList = os.environ
    # For tegra Ubuntu, no special commands needed, but do install Linux prerequisites,
    # do not worry if build-dep does not work, also be sure to apt-get zip as well.
    if (archNum == '32') and (os.name == 'posix') and (os.uname()[1] != 'tegra-ubuntu'):
        # 32-bit shell on Mac OS X 10.6
        if isMac and isSnowLeopard:
            cfgEnvList['CC'] = 'gcc-4.2 -arch i386'
            cfgEnvList['CXX'] = 'g++-4.2 -arch i386'
            cfgEnvList['HOST_CC'] = 'gcc-4.2'
            cfgEnvList['HOST_CXX'] = 'g++-4.2'
            cfgEnvList['RANLIB'] = 'ranlib'
            cfgEnvList['AR'] = 'ar'
            cfgEnvList['AS'] = '$CC'
            cfgEnvList['LD'] = 'ld'
            cfgEnvList['STRIP'] = 'strip -x -S'
            cfgEnvList['CROSS_COMPILE'] = '1'
            cfgCmdList.append('sh')
            cfgCmdList.append(os.path.normpath(configure))
            cfgCmdList.append('--target=i386-apple-darwin8.0.0')
        # 32-bit shell on Mac OS X 10.7 Lion
        elif isMac and isLion:
            cfgEnvList['CC'] = 'clang -Qunused-arguments -fcolor-diagnostics -arch i386'
            cfgEnvList['CXX'] = 'clang++ -Qunused-arguments -fcolor-diagnostics -arch i386'
            cfgEnvList['HOST_CC'] = 'clang -Qunused-arguments -fcolor-diagnostics'
            cfgEnvList['HOST_CXX'] = 'clang++ -Qunused-arguments -fcolor-diagnostics'
            cfgEnvList['RANLIB'] = 'ranlib'
            cfgEnvList['AR'] = 'ar'
            cfgEnvList['AS'] = '$CC'
            cfgEnvList['LD'] = 'ld'
            cfgEnvList['STRIP'] = 'strip -x -S'
            cfgEnvList['CROSS_COMPILE'] = '1'
            cfgCmdList.append('sh')
            cfgCmdList.append(os.path.normpath(configure))
            cfgCmdList.append('--target=i386-apple-darwin8.0.0')
        # 32-bit shell on 32/64-bit x86 Linux
        elif (os.uname()[0] == "Linux") and (os.uname()[4] != 'armv7l'):
            # apt-get `ia32-libs gcc-multilib g++-multilib` first, if on 64-bit Linux.
            cfgEnvList['PKG_CONFIG_LIBDIR'] = '/usr/lib/pkgconfig'
            cfgEnvList['CC'] = 'gcc -m32'
            cfgEnvList['CXX'] = 'g++ -m32'
            cfgEnvList['AR'] = 'ar'
            cfgCmdList.append('sh')
            cfgCmdList.append(os.path.normpath(configure))
            cfgCmdList.append('--target=i686-pc-linux')
        # 32-bit shell on ARM (non-tegra ubuntu)
        elif os.uname()[4] == 'armv7l':
            cfgEnvList['CC'] = '/opt/cs2007q3/bin/gcc'
            cfgEnvList['CXX'] = '/opt/cs2007q3/bin/g++'
            cfgCmdList.append('sh')
            cfgCmdList.append(os.path.normpath(configure))
        else:
            cfgCmdList.append('sh')
            cfgCmdList.append(os.path.normpath(configure))
    # 64-bit shell on Mac OS X 10.7 Lion
    elif (archNum == '64') and (isMac and not isSnowLeopard):
        cfgEnvList['CC'] = 'clang -Qunused-arguments -fcolor-diagnostics'
        cfgEnvList['CXX'] = 'clang++ -Qunused-arguments -fcolor-diagnostics'
        cfgEnvList['AR'] = 'ar'
        cfgCmdList.append('sh')
        cfgCmdList.append(os.path.normpath(configure))
        cfgCmdList.append('--target=x86_64-apple-darwin11.2.0')
    elif (archNum == '64') and (os.name == 'nt'):
        cfgCmdList.append('sh')
        cfgCmdList.append(os.path.normpath(configure))
        cfgCmdList.append('--host=x86_64-pc-mingw32')
        cfgCmdList.append('--target=x86_64-pc-mingw32')
    else:
        cfgCmdList.append('sh')
        cfgCmdList.append(os.path.normpath(configure))

    if compileType == 'dbg':
        cfgCmdList.append('--disable-optimize')
        cfgCmdList.append('--enable-debug')
    elif compileType == 'opt':
        cfgCmdList.append('--enable-optimize')
        cfgCmdList.append('--disable-debug')
        cfgCmdList.append('--enable-profiling')  # needed to obtain backtraces on opt shells

    cfgCmdList.append('--disable-methodjit')
    cfgCmdList.append('--enable-type-inference')
    # Fuzzing tweaks for more useful output, bug 706433
    cfgCmdList.append('--enable-more-deterministic')
    cfgCmdList.append('--disable-tests')

    if os.name != 'nt':
        if ((os.uname()[0] == "Linux") and (os.uname()[4] != 'armv7l')) or isMac:
            cfgCmdList.append('--enable-valgrind')
            # ccache does not seem to work on Mac.
            if not isMac:
                cfgCmdList.append('--with-ccache')
        # ccache is not applicable for Windows and non-Tegra Ubuntu ARM builds.
        elif os.uname()[1] == 'tegra-ubuntu':
            cfgCmdList.append('--with-ccache')
            cfgCmdList.append('--with-arch=armv7-a')

    if threadsafe:
        cfgCmdList.append('--enable-threadsafe')
        cfgCmdList.append('--with-system-nspr')
    # Works-around "../editline/libeditline.a: No such file or directory" build errors by using
    # readline instead of editline.
    #cfgCmdList.append('--enable-readline')

    if os.name == 'nt':
        # Only tested to work for pymake.
        counter = 0
        for entry in cfgCmdList:
            if os.sep in entry:
                cfgCmdList[counter] = cfgCmdList[counter].replace(os.sep, '\\\\')
            counter = counter + 1

    verboseDump('This is the configure command (environment variables not included):')
    verboseDump('%s\n' % ' '.join(cfgCmdList))

    if os.name == 'nt':
        nullLocation = open('nul', 'w')
    else:
        nullLocation = open('/dev/null', 'w')

    # If on Windows, be sure to first install prerequisites at https://developer.mozilla.org/En/Windows_SDK_versions
    # Note that on Windows, redirecting stdout to subprocess.STDOUT does not work on Python 2.6.5.
    if verbose:
        subprocess.call(cfgCmdList, stderr=subprocess.STDOUT, cwd=objdir, env=cfgEnvList)
    else:
        subprocess.call(cfgCmdList, stdout=nullLocation, stderr=subprocess.STDOUT, cwd=objdir, env=cfgEnvList)

def binaryPostfix():
    if os.name == 'posix':
        return ''
    elif os.name == 'nt':
        return '.exe'

def shellName(archNum, compileType, extraID, valgrindSupport):
    if os.name == 'posix':
        osname = os.uname()[0].lower()
    elif os.name == 'nt':
        osname = os.name.lower()
    vgmark = "-vg" if valgrindSupport else ""
    return 'js-' + compileType + '-' + archNum + vgmark + '-' + extraID + '-' + osname + binaryPostfix()

def compileCopy(archNum, compileType, extraID, usePymake, destDir, objdir, valgrindSupport):
    '''
    This function compiles and copies a binary.
    '''
    jobs = (cpu_count() * 5) // 4
    compiledName = os.path.join(objdir, 'js' + binaryPostfix())
    try:
        if usePymake:
            out = captureStdout(['python', '-OO', os.path.normpath(os.path.join(globalRepo, 'build', 'pymake', 'make.py')), '-j' + str(jobs), '-s'], combineStderr=True, currWorkingDir=objdir)
            # Pymake in builds earlier than revision 232553f741a0 did not support the '-s' option.
            if 'no such option: -s' in out:
                out = captureStdout(['python', '-OO', os.path.normpath(os.path.join(globalRepo, 'build', 'pymake', 'make.py')), '-j' + str(jobs)], combineStderr=True, currWorkingDir=objdir)
        else:
            out = captureStdout(['make', '-C', objdir, '-j' + str(jobs), '-s'], combineStderr=True, ignoreExitCode=True, currWorkingDir=objdir)
    except:
        # Sometimes a non-zero error can be returned during the make process, but eventually a
        # shell still gets compiled.
        if os.path.exists(compiledName):
            print 'A shell was compiled even though there was a non-zero exit code returned. Continuing...'
        else:
            print out
            raise Exception("Running 'make' did not result in a js shell")

    if not os.path.exists(compiledName):
        print out
        raise Exception("Running 'make' did not result in a js shell")
    else:
        newName = os.path.join(destDir, shellName(archNum, compileType, extraID, valgrindSupport))
        shutil.copy2(compiledName, newName)
        return newName

def cpUsefulFiles(filePath):
    '''
    This function copies over useful files that are updated in hg fuzzing branch.
    '''
    if 'Windows-XP' not in platform.platform():
        filePath = os.path.expanduser(filePath)
    shutil.copy2(filePath, '.')

####################
#  Test Functions  #
####################

def archOfBinary(b):
    '''
    This function tests if a binary is 32-bit or 64-bit.
    '''
    unsplitFiletype = captureStdout(['file', b])
    filetype = unsplitFiletype.split(':', 1)[1]
    if 'universal binary' in filetype:
        raise Exception("I don't know how to deal with multiple-architecture binaries")
    if '386' in filetype or '32-bit' in filetype:
        assert '64-bit' not in filetype
        return '32'
    if '64-bit' in filetype:
        assert '32-bit' not in filetype
        return '64'

def createTestFiles(name, contentsLineList):
    testFile = open(name, 'w')
    for line in contentsLineList:
        testFile.writelines(line)
    testFile.close()

    if not os.path.isfile(name):
        raise Exception(name, 'does not exist.')

def exitCodeDbgOptOrJsShellXpcshell(shell, dbgOptOrJsShellXpcshell):
    '''
    This function returns the exit code after testing the shell.
    '''
    contents = []
    if dbgOptOrJsShellXpcshell == 'dbgOpt':
        testFilename = 'dbgOptTest.js'
        contents.append('gczeal()')
    elif dbgOptOrJsShellXpcshell == 'jsShellXpcshell':
        testFilename = 'jsShellXpcshellTest.js'
        contents.append('Components')
    createTestFiles(testFilename, contents)

    while True:
        try:
            if os.name == 'posix':
                testFileExitCode = subprocess.call([shell, testFilename])
            elif os.name == 'nt':
                testFileExitCode = subprocess.call([shell, testFilename], shell=True)
        except:
            # xpcshells need another argument after run-mozilla.sh
            if os.name == 'posix':
                testFileExitCode = subprocess.call([shell, './xpcshell', testFilename])
            elif os.name == 'nt':
                testFileExitCode = subprocess.call([shell, './xpcshell', testFilename], shell=True)
        finally:
            os.remove(testFilename)  # Remove testfile after grabbing the error code.
            break

    return testFileExitCode

def testJsShellOrXpcshell(shellName):
    '''
    This function tests if a binary is a js shell or xpcshell.
    '''
    exitCode = exitCodeDbgOptOrJsShellXpcshell(shellName, 'jsShellXpcshell')

    # The error code for xpcshells when passing in the Components function should be 0.
    if exitCode == 0:
        return 'xpcshell'
    # js shells don't have Components compiled in by default.
    elif exitCode == 3:
        return 'jsShell'
    else:
        raise Exception('Unknown exit code after testing if js shell or xpcshell: ' + str(exitCode))

def testDbgOrOpt(jsShellName):
    '''
    This function tests if a binary is a debug or optimized shell.
    '''
    exitCode = exitCodeDbgOptOrJsShellXpcshell(jsShellName, 'dbgOpt')

    # The error code for debug shells when passing in the gczeal() function should be 0.
    if exitCode == 0:
        return 'dbg'
    # Optimized shells don't have gczeal() compiled in by default.
    elif exitCode == 3:
        return 'opt'
    else:
        raise Exception('Unknown exit code after testing if debug or opt: ' + exitCode)

def testDbgOrOptGivenACompileType(jsShellName, compileType):
    '''
    This function tests if a binary is a debug or optimized shell given a compileType.
    '''
    exitCode = exitCodeDbgOptOrJsShellXpcshell(jsShellName, 'dbgOpt')

    verboseDump('The error code for debug shells should be 0.')
    verboseDump('The error code for opt shells should be 3.')
    verboseDump('The actual error code for ' + jsShellName + ' now, is: ' + str(exitCode))

    # The error code for debug shells when passing in the gczeal() function should be 0.
    if compileType == 'dbg' and exitCode != 0:
        print 'ERROR: A debug shell when tested with the gczeal() should return "0" as the error code.'
        print 'compileType is: ' + compileType
        print 'exitCode is: ' + str(exitCode)
        print
        raise Exception('The compiled binary is not a debug shell.')
    # Optimized shells don't have gczeal() compiled in by default.
    elif compileType == 'opt' and exitCode != 3:
        print 'ERROR: An optimized shell when tested with the gczeal() should return "3" as the error code.'
        print 'compileType is: ' + compileType
        print 'exitCode is: ' + str(exitCode)
        print
        raise Exception('The compiled binary is not an optimized shell.')

def timeShellFunction(command, cwd=os.getcwdu()):
    print 'Running `%s` now..' % ' '.join(command)
    startTime = time.time()
    retVal = subprocess.call(command, cwd=cwd)
    endTime = time.time()
    print '`' + ' '.join(command) + '` took %.3f seconds.\n' % (endTime - startTime)
    return retVal

if __name__ == '__main__':
    pass
