#!/usr/bin/python

# The following is so that insights-client continues to work normally in places where
# Docker is not installed.
#
# Note that this is actually testing if the python docker client is importable (is installed),
# and if the docker server on this machine is accessable, which isn't exactly the
# same thing as 'there is no docker on this machine'.

import logging
import shlex
import subprocess

from redhat_access_insights.constants import InsightsConstants as constants

APP_NAME = constants.app_name
logger = logging.getLogger(APP_NAME)


def run_command_very_quietly(cmdline):
    # this takes a string (not an array)
    # need to redirect stdout and stderr to /dev/null
    cmd = shlex.split(cmdline)
    proc = subprocess.Popen(cmd)
    returncode = proc.wait()
    return returncode


# Check to see if we have access to docker through the "docker" python module
HaveDocker = False
HaveDockerException = None
try:
    import docker
    docker.Client(base_url='unix://var/run/docker.sock').images()
    HaveDocker = True

except Exception as e:
    HaveDockerException = e

# Check to see if we have access to Atomic through the 'atomic' command
HaveAtomic = False
HaveAtomicException = None
try:
    if run_command_very_quietly("atomic --version") == 0:
        # a returncode of 0 means cmd ran correctly
        HaveAtomic = True
    else:
        # anything else indicates problem
        HaveAtomic = False
except Exception as e:
    # this happens when atomic isn't installed or is otherwise unrunable
    HaveAtomic = False
    HaveAtomicException = e

HaveAtomicMount = HaveAtomic


if HaveDocker:
    import os
    import tempfile
    import shutil

    from redhat_access_insights.client_config import InsightsClient

    def runcommand(cmd):
        # this takes an array (not a string)
        logger.debug("Running Command: %s" % cmd)
        proc = subprocess.Popen(cmd)
        returncode = proc.wait()
        return returncode

    def get_container_name():
        return "insights-client"

    def get_image_name():
        if InsightsClient.options.docker_image_name:
            logger.debug("found docker_image_name in options: %s" % InsightsClient.options.docker_image_name)
            return InsightsClient.options.docker_image_name

        elif InsightsClient.config.get(APP_NAME, 'docker_image_name'):
            logger.debug("found docker_image_name in config: %s" % InsightsClient.config.get(APP_NAME, 'docker_image_name'))
            return InsightsClient.config.get(APP_NAME, 'docker_image_name')

        else:
            logger.debug("found docker_image_name in constants: %s" % constants.docker_image_name)
            return constants.docker_image_name

    def use_atomic_run():
        return HaveAtomic

    def use_atomic_mount():
        return HaveAtomicMount

    def pull_image(image):
        return runcommand(shlex.split("docker pull") + [image])

    def insights_client_container_is_available():
        image_name = get_image_name()
        if image_name:
            client = docker.Client(base_url='unix://var/run/docker.sock')

            pull_image(image_name)

            images = client.images(image_name)
            if len(images) == 0:
                logger.debug("insights-client docker image not available: %s" % image_name)
                return False
            else:
                return True
        else:
            return False

    def get_targets():
        client = docker.Client(base_url='unix://var/run/docker.sock')
        targets = []
        if client:
            for d in client.images(quiet=True):
                targets.append({'type': 'docker_image', 'name': d})
            for d in client.containers(all=True, trunc=False):
                targets.append({'type': 'docker_container', 'name': d['Id']})
        return targets

    def run_in_container():

        if InsightsClient.options.from_file:
            logger.error('--from-file is incompatible with transfering to a container.')
            return 1

        if use_atomic_run():
            return runcommand(["atomic", "run", "--name", get_container_name(), get_image_name(), "redhat-access-insights", "--run-here"] + InsightsClient.argv[1:])
        else:
            run_string = _get_run_string(get_image_name(), get_container_name())
            if not run_string:
                logger.debug("docker RUN label not found in image " + get_image_name() + " using fallback RUN string")
                run_string = "docker run --privileged=true -i -a stdin -a stdout -a stderr --rm -v /var/run/docker.sock:/var/run/docker.sock -v /var/lib/docker/:/var/lib/docker/ -v /dev/:/dev/ -v /etc/redhat-access-insights/:/etc/redhat-access-insights -v /etc/pki/:/etc/pki/ " + get_image_name()

            docker_args = shlex.split(run_string + " redhat-access-insights")

            return runcommand(docker_args + ["--run-here"] + InsightsClient.argv[1:])

    def _get_run_string(imagename, containername):
        labelstring = _get_label(imagename, "RUN")
        if labelstring:
            if containername:
                labelstring = labelstring.replace(" --name NAME", " --name " + containername)
            else:
                labelstring = labelstring.replace(" --name NAME", " ")

            labelstring = labelstring.replace("IMAGE", imagename)
            return labelstring

        return None

    def _get_label(imagename, label):
        imagedata = _inspect_image(imagename)
        if imagedata:
            idx = ("Config", "Labels", label)
            if dictmultihas(imagedata, idx):
                return dictmultiget(imagedata, idx)

        return None

    def _inspect_image(image_name):
        client = docker.Client(base_url='unix://var/run/docker.sock')
        try:
            return client.inspect_image(image_name)
        except Exception as e:
            logger.debug("Not able to inspect image %s: %s" % (image_name, e))

        return None

    class AtomicTemporaryMountPoint:
        # this is used for both images and containers
        def __init__(self, image_id, mount_point):
            self.image_id = image_id
            self.mount_point = mount_point

        def get_fs(self):
            return self.mount_point

        def close(self):
            try:
                logger.debug("Closing Id %s On %s" % (self.image_id, self.mount_point))
                runcommand(shlex.split("atomic unmount") + [self.mount_point])
            except Exception as e:
                logger.debug("exception while unmounting image or container: %s" % e)
            shutil.rmtree(self.mount_point, ignore_errors=True)

    from mount import DockerMount, Mount, MountError

    class DockerTemporaryMountPoint:
        # this is used for both images and containers
        def __init__(self, client, image_id, mount_point, cid):
            self.client = client
            self.image_id = image_id
            self.mount_point = mount_point
            self.cid = cid

        def get_fs(self):
            return self.mount_point

        def close(self):
            try:
                logger.debug("Closing Id %s On %s" % (self.image_id, self.mount_point))
                # If using device mapper, unmount the bind-mount over the directory
                if self.client.info()['Driver'] == 'devicemapper':
                    Mount.unmount_path(self.mount_point)

                DockerMount(self.mount_point).unmount(self.cid)
            except Exception as e:
                logger.debug("exception while unmounting image or container: %s" % e)
            shutil.rmtree(self.mount_point, ignore_errors=True)

    def open_image(image_id):
        global HaveAtomicException
        if HaveAtomicException:
            logger.debug("atomic is either not installed or not accessable %s" % HaveAtomicException);
            HaveAtomicException = None

        if use_atomic_mount():
            mount_point = tempfile.mkdtemp()
            logger.debug("Opening Image Id %s On %s using atomic" % (image_id, mount_point))
            if runcommand(shlex.split("atomic mount") + [image_id, mount_point]) == 0:
                return AtomicTemporaryMountPoint(image_id, mount_point)
            else:
                logger.error('Could not mount Image Id %s On %s' % (image_id, mount_point))
                shutil.rmtree(mount_point, ignore_errors=True)
                return None

        else:
            client = docker.Client(base_url='unix://var/run/docker.sock')
            if client:
                mount_point = tempfile.mkdtemp()
                logger.debug("Opening Image Id %s On %s using docker client" % (image_id, mount_point))
                # docker mount creates a temp image
                # we have to use this temp image id to remove the device
                mount_point, cid = DockerMount(mount_point).mount(image_id)
                if client.info()['Driver'] == 'devicemapper':
                    DockerMount.mount_path(os.path.join(mount_point, "rootfs"), mount_point, bind=True)

                if cid:
                    return DockerTemporaryMountPoint(client, image_id, mount_point, cid)
                else:
                    logger.error('Could not mount Image Id %s On %s' % (image_id, mount_point))
                    shutil.rmtree(mount_point, ignore_errors=True)
                    return None

            else:
                logger.error('Could not connect to docker to examine image %s' % image_id)
                return None

    def open_container(container_id):
        global HaveAtomicException
        if HaveAtomicException:
            logger.debug("atomic is either not installed or not accessable %s" % HaveAtomicException);
            HaveAtomicException = None

        if use_atomic_mount():
            mount_point = tempfile.mkdtemp()
            logger.debug("Opening Container Id %s On %s using atomic" % (container_id, mount_point))
            if runcommand(shlex.split("atomic mount") + [container_id, mount_point]) == 0:
                return AtomicTemporaryMountPoint(container_id, mount_point)
            else:
                logger.error('Could not mount Container Id %s On %s' % (container_id, mount_point))
                shutil.rmtree(mount_point, ignore_errors=True)
                return None

        else:
            client = docker.Client(base_url='unix://var/run/docker.sock')
            if client:
                mount_point = tempfile.mkdtemp()
                logger.debug("Opening Container Id %s On %s using docker client" % (container_id, mount_point))
                # docker mount creates a temp image
                # we have to use this temp image id to remove the device
                mount_point, cid = DockerMount(mount_point).mount(container_id)
                if client.info()['Driver'] == 'devicemapper':
                    DockerMount.mount_path(os.path.join(mount_point, "rootfs"), mount_point, bind=True)
                if cid:
                    return DockerTemporaryMountPoint(client, container_id, mount_point, cid)
                else:
                    logger.error('Could not mount Container Id %s On %s' % (container_id, mount_point))
                    shutil.rmtree(mount_point, ignore_errors=True)
                    return None

            else:
                logger.error('Could not connect to docker to examine container %s' % container_id)
                return None

else:
    # If we can't import docker then we stub out all the main functions to report errors

    def insights_client_container_is_available():
        # Don't print error here, this is the way to tell if running in a container is possible
        # but do print debug info
        logger.debug('not transfering to insights-client image')
        logger.debug('Docker is either not installed or not accessable: %s' %
                     (HaveDockerException if HaveDockerException else ''))
        return False

    def run_in_container():
        logger.debug('Could not connect to docker to transfer into a container')
        logger.error('Docker is either not installed or not accessable: %s' %
                     (HaveDockerException if HaveDockerException else ''))
        return 1

    def get_targets():
        logger.debug('Could not connect to docker to collect from images and containers')
        logger.debug('Docker is either not installed or not accessable: %s' %
                     (HaveDockerException if HaveDockerException else ''))
        return []

    def open_image(image_id):
        logger.error('Could not connect to docker to examine image %s' % image_id)
        logger.error('Docker is either not installed or not accessable: %s' %
                     (HaveDockerException if HaveDockerException else ''))
        return None

    def open_container(container_id):
        logger.error('Could not connect to docker to examine container %s' % container_id)
        logger.error('Docker is either not installed or not accessable: %s' %
                     (HaveDockerException if HaveDockerException else ''))
        return None

#
# JSON data has lots of nested dictionaries, that are often optional.
#
# so for example you want to write:
#
#    foo = d['meta_specs']['uploader_log']['something_else']
#
# but d might not have 'meta_specs' and that might not have 'uploader_log' and ...
# so write this instead
#
#   idx = ('meta_specs','uploader_log','something_else')
#   if dictmultihas(d, idx):
#      foo = dictmultiget(d, idx)
#   else:
#      ....
#


def dictmultihas(d, idx):
    # 'idx' is a tuple of strings, indexing into 'd'
    #  if d doesn't have these indexes, return False
    for each in idx[:-1]:
        if d and each in d:
            d = d[each]
    if d and len(idx) > 0 and idx[-1] in d:
        return True
    else:
        return False


def dictmultiget(d, idx):
    # 'idx' is a tuple of strings, indexing into 'd'
    for each in idx[:-1]:
        d = d[each]
    return d[idx[-1]]
