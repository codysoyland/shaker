"""
Build and lauch EBS instances as salt minions.
"""
from shaker.version import __version__

import os
import time
import optparse
import email.mime
import boto
import boto.ec2
from boto.ec2.blockdevicemapping import EBSBlockDeviceType, BlockDeviceMapping
import shaker.log
import shaker.config
import shaker.template
LOG = shaker.log.getLogger(__name__)
RUN_INSTANCE_TIMEOUT = 120 # seconds


class EBSFactory(object):
    """EBSFactory - build and launch EBS salt minions.
    """
    def __init__(self):
        cli, config_dir, arg0 = self.parse_cli()
        self.profile = shaker.config.user_profile(
            arg0,
            cli,
            config_dir)
        self.test_mode = cli.test_mode
        self.config = dict(self.profile)
        self.config['config_dir'] = config_dir

    def process(self):
        self.user_data = self.build_mime_multipart()
        LOG.debug("User Data Follows ..................\n{0}".format(
            self.user_data))
        self.conn = self.get_connection()
        if self.verify_settings() and not self.test_mode:
            self.launch_instance()
            if self.config['assign_dns']:
                self.assign_dns(self.config['assign_dns'])

    def get_connection(self):
        regions = boto.ec2.regions()
        region=[x.name for x in regions if x.name.startswith(
            self.config['ec2_zone'][:-1])][0]
        return regions[[x.name for x in regions].index(region)].connect()

    def launch_instance(self):
        if not self.verify_settings():
            return
        block_map = BlockDeviceMapping()
        root_device = self.config['ec2_root_device']
        block_map[root_device] = EBSBlockDeviceType()
        if self.config['ec2_size']:
            block_map[root_device].size = self.config['ec2_size']
        block_map[root_device].delete_on_termination = True
        reservation = self.conn.run_instances(
            self.config['ec2_ami_id'],
            key_name=self.config['ec2_key_name'],
            security_groups=[self.config['ec2_security_group']],
            instance_type=self.config['ec2_instance_type'],
            placement=self.config['ec2_zone'],
            monitoring_enabled=self.config['ec2_monitoring_enabled'],
            block_device_map=block_map,
            user_data=self.user_data)
        self.instance = reservation.instances[0]
        if self.config['hostname']:
            self.assign_name_tag()
        secs = RUN_INSTANCE_TIMEOUT
        rest_interval = 5
        while secs and not self.instance.state == 'running':
            time.sleep(rest_interval)
            secs = secs - rest_interval
            try:
                self.instance.update()
            except boto.exception.EC2ResponseError:
                pass
        if secs <= 0:
            errmsg = "run instance %s failed after %d seconds" % (
                self.instance.id, RUN_INSTANCE_TIMEOUT)
            LOG.error(errmsg)
        else:
            msg1 = "Started Instance: {0}\n".format(self.instance.id)
            LOG.info(msg1)
            print msg1
            p = int(self.config['ssh_port'])
            port = "-p {0} ".format(p) if p and not p == 22 else ''
            ## change user to 'root' for all non-Ubuntu systems
            user = self.config['sudouser'] if self.config['sudouser'] and self.config['ssh_import'] else 'ubuntu'
            #XXX - TODO: replace public dns with fqdn, where appropriate
            msg2 = "To access: ssh {0}{1}@{2}\nTo terminate: shaker-terminate {3}".format(
                port,
                user,
                self.instance.public_dns_name,
                self.instance.id)
            LOG.info(msg2)
            print msg2

    def assign_name_tag(self):
        """Assign the 'Name' tag to the instance, but only if it isn't already in use."""
        tag = self.config['hostname']
        for reservation in self.conn.get_all_instances():
                for i in reservation.instances:
                    if tag == i.tags.get('Name'):
                        return
        self.instance.add_tag('Name', tag)

    def build_mime_multipart(self):
        userData = shaker.template.UserData(self.config)
        outer = email.mime.multipart.MIMEMultipart()
        for content, subtype, filename in [
            (userData.user_script, 'x-shellscript', 'user-script.txt'),
            (userData.cloud_init, 'cloud-config', 'cloud-config.txt'),]:
            msg = email.mime.text.MIMEText(content, _subtype=subtype)
            msg.add_header('Content-Disposition',
                           'attachment',
                           filename=filename)
            outer.attach(msg)
        return outer.as_string()

    #XXX - Possibly move this to its own module
    def verify_settings(self):
        if not self.config['ec2_ami_id']:
            LOG.error("Missing ec2_ami_id")
            return False
        if not self.config['ec2_key_name']:
            # If no key pair has been specified, just use the first one,
            # iff it's the only one.
            key_pairs = self.conn.get_all_key_pairs()
            if len(key_pairs) > 1:
                LOG.error("Missing ec2_key_name parameter")
                return False
            self.config['ec2_key_name'] = [kp.name for kp in key_pairs][0]
        #XXX TODO: add error handling, logging for ec2_size
        if self.config['ec2_size']:
            try:
                self.config['ec2_size'] = int(self.config['ec2_size'])
            except ValueError:
                self.config['ec2_size'] = 0
        # verify ec2_instance_type in:
            # t1.micro
            # m1.small  (default)
            # m2.xlarge, m2.2xlarge, m2.4xlarge
            # c1.medium, c1.xlarge, cc1.4xlarge, cc2.8xlarge
        return True

    def parse_cli(self):
        parser = optparse.OptionParser(
            usage="%prog [options] profile",
            version="%%prog %s" % __version__,
        )
        parser.add_option('-a', '--ami', dest='ec2_ami_id', metavar='AMI',
                          help='build instance from AMI')
        parser.add_option('--ec2-group', dest='ec2_security_group')
        parser.add_option('--ec2-zone', dest='ec2_zone', default='')
        parser.add_option('--config-dir', dest='config_dir', help="configuration directory")
        parser.add_option('-t', '--test', dest='test_mode',
                          action='store_true', default=False,
                          help='test mode')
        import shaker.log
        parser.add_option('-l',
                '--log-level',
                dest='log_level',
                default='warning',
                choices=shaker.log.LOG_LEVELS.keys(),
                help='Log level: %s.  \nDefault: %%default' %
                     ', '.join(shaker.log.LOG_LEVELS.keys())
                )
        (opts, args) = parser.parse_args()
        if len(args) < 1:
            print parser.format_help().strip()
            if opts.terminate_instance:
                errmsg = "\nError: Specify instance ID to terminate"
            else:
                errmsg = "\nError: Specify shaker profile"
            raise SystemExit(errmsg)
        import shaker.config
        config_dir = shaker.config.get_config_dir(opts.config_dir)
        shaker.log.start_logger(
            __name__,
            os.path.join(config_dir, 'shaker.log'),
            opts.log_level)
        return opts, config_dir, args[0]
