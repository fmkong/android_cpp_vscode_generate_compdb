from __future__ import print_function

import argparse
import json
import os
import re
import shlex
import sys
import logging
import sqlite3

__version__ = "0.2.2"
logger = logging.getLogger()
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler(sys.stdout)
console_formatter = logging.Formatter(fmt='%(asctime)s [%(levelname)s] [%(name)s:%(lineno)d] %(message)s')
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

MAKEFILE_TYPE_NONE = 0
MAKEFILE_TYPE_BP = 1
MAKEFILE_TYPE_MK = 2

def parse_arguments(command = str()):
    arg_list = []
    args = command.split()
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-I":
            arg += format("%s" %(args[i + 1]))
            i += 1

        i += 1
        if arg == "$":
            continue
        if arg in ["-fexperimental-new-pass-manager"]:
            continue
        arg = arg.replace('\"', '"').replace('\\', '').replace('\"\"', "\"")
        arg_list.append(arg)
    return arg_list

class MakefileObj:
    LOCAL_MODULE_DEF_PATTERN = re.compile(r'^\s*LOCAL_MODULE\s*:=\s*(.+)$')
    def __init__(self, path = ""):
        if not os.path.exists(path):
            logging.error("not exit make file: %s" %(path))
            raise Exception("path not exist")
        self.makefile = path
        self.local_modules = []
        self.makefile_type = MAKEFILE_TYPE_NONE
        self.parse_makefile()

    def parse_mk_file(self):
        with open(self.makefile, "r") as makefile:
            for line in makefile:
                match = self.LOCAL_MODULE_DEF_PATTERN.match(line)
                if match:
                    logging.info("found LOCAL_MODULE: %s" %(match.group(1).strip()))
                    self.local_modules.append(match.group(1).strip())

    def parse_bp_file(self):
        PATTERN = re.compile(r'\s*name\s*:\s*"(.+)"')
        with open(self.makefile, "r") as makefile:
            for line in makefile:
                match = PATTERN.match(line)
                if match:
                    module_name = re.sub("\",", "", match.group(1).strip())
                    logging.info("found LOCAL_MODULE: %s" %(module_name))
                    self.local_modules.append(module_name)

    def parse_makefile(self):
        if self.makefile.endswith('.mk'):
            self.makefile_type = MAKEFILE_TYPE_MK
            return self.parse_mk_file()
        elif self.makefile.endswith('.bp'):
            self.makefile_type = MAKEFILE_TYPE_BP
            return self.parse_bp_file()

    def get_local_modules(self):
        return self.local_modules


class NinjaFileObj:
    compdb = []
    def __init__(self, target, root_dir):
        self.target_ninja = os.path.join(root_dir, format("out/build-%s.ninja" %(target)))
        if not os.path.exists(self.target_ninja):
            logging.error("not exit build target ninja file: %s" %(self.target_ninja))
            raise Exception("path not exist : %s" %(self.target_ninja))
        self.soong_ninja = os.path.join(root_dir, "out/soong/build.ninja")
        if not os.path.exists(self.soong_ninja):
            logging.error("not exit soong/build.ninja file: %s" %(self.soong_ninja))
            raise Exception("path not exist : %s" %(self.soong_ninja))
        self.root_dir = os.path.abspath(root_dir)

        self.target_ninja_obj = BuildTargetNinjaForAndroidMK(self.target_ninja, root_dir, self.compdb)
        self.soong_ninja_obj = BuildNinjaForAndroidBP(self.soong_ninja, root_dir, self.compdb)

    def save_compile_db_file(self):
        with open('../compile_commands.json', 'w') as compdb_file:
            json.dump(self.compdb, compdb_file, indent=1)

    def get_build_cmd_for_module(self, module, makefile_type):
        if makefile_type == MAKEFILE_TYPE_MK:
            self.target_ninja_obj.get_build_cmd_for_module(module)
        elif makefile_type == MAKEFILE_TYPE_BP:
            self.soong_ninja_obj.get_build_cmd_for_module(module)


class BuildNinjaForAndroidBP:
    def __init__(self, path, root_dir, compdb):
        self.ninja_file = path
        self.root_dir = os.path.abspath(root_dir)
        self.compdb = compdb
        # self.module_db_path = "test.db"
        self.module_db_path = ":memory:"
        self.db_conn = None
        self.db_cur = None

        # self.generate_var_dict_and_module_index()

    def __del__(self):
        if self.db_cur:
            self.db_cur.close()
        if self.db_conn:
            self.db_conn.close()

    def create_or_connect_db_file(self):
        if os.path.exists(self.module_db_path):
            self.db_conn = sqlite3.connect(self.module_db_path)
            self.db_cur = self.db_conn.cursor()
            return True #true for db is ready
        self.db_conn = sqlite3.connect(self.module_db_path)
        self.db_cur = self.db_conn.cursor()
        sql_text_create_module_table = '''CREATE TABLE modules
           (name TEXT,
            variant TEXT,
            block TEXT);'''
        self.db_cur.execute(sql_text_create_module_table)
        sql_text_create_variables_table = '''CREATE TABLE variables
           (name TEXT,
            value TEXT);'''
        self.db_cur.execute(sql_text_create_variables_table)
        return False #false for db is not ready

    def generate_var_dict_and_module_index(self):
        MODULE_FIRST_LINE_PATTERN = re.compile(r'^#\s*Module:\s*([\w+\.@\-]{1,})\s*')
        MODULE_LAST_LINE_PATTERN = re.compile(r'^\s*[# ]{50,}$')
        VARIANT_PATTERN = re.compile(r'^#\s*Variant:\s*([\w+.-]{1,})\s*')
        VARS_PATTERN = re.compile(r'^\s{0,}([\w+.-]{1,})\s*=\s*(.*)$')
        lines = ""
        collect_str = False
        module_name = ""
        variant_name = ""

        if self.create_or_connect_db_file():
            return

        logger.info("preparing module dict and vars...")
        with open(self.ninja_file) as ninja_file:
            for line in ninja_file:
                # line = self.expand_variables_in_line(line)
                match = VARS_PATTERN.match(line)
                if match:
                    sql_text_insert_module = "INSERT INTO variables VALUES(?, ?)"
                    # print((match.group(1), match.group(2)))
                    self.db_cur.execute(sql_text_insert_module, (str(match.group(1)), str(match.group(2))))
                match = MODULE_FIRST_LINE_PATTERN.match(line)
                if match:
                    collect_str = True
                    module_name = match.group(1)

                match = VARIANT_PATTERN.match(line)
                if match:
                    collect_str = True
                    variant_name = match.group(1)

                if collect_str:
                    lines += line

                if collect_str and MODULE_LAST_LINE_PATTERN.match(line):
                    sql_text_insert_module = "INSERT INTO modules VALUES(?, ?, ?)"
                    # print((module_name, variant_name, lines))
                    self.db_cur.execute(sql_text_insert_module,  (module_name, variant_name, lines))
                    collect_str = False
                    lines = ""
                    module_name = ""
                    variant_name = ""
        self.db_conn.commit()
        logger.info("finished...")

    def expand_variables_in_line(self, line):
        VAR_REF_PATTERN = re.compile(r'\$\{([\w+.-]{1,})\}')
        for match in VAR_REF_PATTERN.finditer(line):
            sql_text_select_var = "SELECT * FROM variables WHERE name = (?)"
            self.db_cur.execute(sql_text_select_var, (match.group(1),))
            value = self.db_cur.fetchone()

            if value and match.group(1) == value[0]:
                line = line.replace(match.group(0), value[1])
                # print("replace %s to %s" %(match.group(0), value[1]))
            else:
                line = line.replace(match.group(0), "")
                print("ignore VAR_REF %s" %(match.group(0)))

        match = VAR_REF_PATTERN.search(line)
        if match:
            # print("expand match %s, line %s" %(match.group(1), line))
            line = self.expand_variables_in_line(line)
        return line

    def parse_command(self, src_file, cc_cmd, c_flags):
        self.compdb.append({
            'directory': self.root_dir,
            # 'command': command,
            'file': src_file,
            'arguments': parse_arguments(cc_cmd + " " + c_flags)
        })
        return True

    def process_build_cmds(self, build_dict):
        if 'c_flags' in build_dict and 'build_src' in build_dict and 'cc_cmd' in build_dict:
            logger.debug("process build cmd for: %s" %(build_dict['build_src']))
            self.parse_command(build_dict['build_src'], build_dict['cc_cmd'], build_dict['c_flags'])

    def analysis_build_for_module(self, module):
        BUILD_PATTERN = re.compile(r'^\s*build \$\s*')
        BUILD_TARGET_PATTERN = re.compile(r'^\s*([\w+\/._-]{1,})\s*:\s*([\w+\/._-]{1,})\s*\$')
        BUILD_SRC_PATTERN = re.compile(r'^\s*([\w+\/._-]{1,})\s*\|\s*([\w+\/._-]{1,})\s*(.*)')
        CC_CMD_PATTERN = re.compile(r'^\s*ccCmd\s*=\s*([\w+\/._-]{1,})\s*')
        LD_CMD_PATTERN = re.compile(r'^\s*ldCmd\s*=\s*([\w+\/._-]{1,})\s*')
        C_FLAGS_PATTERN = re.compile(r'^\s*cFlags\s*=(.*)')
        LD_FLAGS_PATTERN = re.compile(r'^\s*ldFlags\s*=(.*)')
        lines = ""
        sql_text_select_module = "SELECT * FROM modules WHERE name = (?)"
        self.db_cur.execute(sql_text_select_module, (module,))
        # select the first one
        # for module_info in self.db_cur.fetchall():
        module_info = self.db_cur.fetchall()
        if len(module_info) > 0:
            module_info = module_info[0]
        else:
            return
        lines += module_info[2]
        lines_new = ""
        build_dict = {}

        for line in lines.splitlines():
            line = self.expand_variables_in_line(line)
            if BUILD_PATTERN.match(line):
                if len(build_dict):
                    self.process_build_cmds(build_dict)
                build_dict = {}

            match = BUILD_TARGET_PATTERN.match(line)
            if 'build_target' not in build_dict and match:
                build_dict['build_target'] = match.group(1)

            match = BUILD_SRC_PATTERN.match(line)
            if 'build_src' not in build_dict and match:
                build_dict['build_src'] = match.group(1)

            match = CC_CMD_PATTERN.match(line)
            if 'cc_cmd' not in build_dict and match:
                build_dict['cc_cmd'] = match.group(1)

            match = LD_CMD_PATTERN.match(line)
            if 'ld_cmd' not in build_dict and match:
                build_dict['ld_cmd'] = match.group(1)

            match = C_FLAGS_PATTERN.match(line)
            if 'c_flags' not in build_dict and match:
                build_dict['c_flags'] = match.group(1)

            match = LD_FLAGS_PATTERN.match(line)
            if 'ld_flags' not in build_dict and match:
                build_dict['ld_flags'] = match.group(1)

            lines_new += line + "\n"

        if len(build_dict):
            self.process_build_cmds(build_dict)

    def get_build_cmd_for_module(self, module):
        if not self.db_conn:
            self.generate_var_dict_and_module_index()

        self.analysis_build_for_module(module)

class BuildTargetNinjaForAndroidMK:
    RULE_PATTERN = re.compile(r'^\s*rule\s+(\S+)$')
    DESCRIPTION_PATTERN = re.compile(r'^\s*description\s*=\s*(.+)$')
    COMMAND_PATTERN = re.compile(r'^\s*command\s*=\s*(.+)$')
    BUILD_PATTERN = re.compile(r'^\s*build\s+.*:\s*(?P<rule>\S+)\s+(?P<file>\S+)')
    CAT_PATTERN = re.compile(r'\\\$\$\(\s*cat\s+([^\)]+)\)')
    SUBCOMMAND_PATTERN = re.compile(r'\(([^\)]*)\)')

    rules = {}
    cat_cache = {}

    def __init__(self, path, root_dir, compdb):
        self.ninja_file = path
        self.root_dir = os.path.abspath(root_dir)
        self.compdb = compdb

    def cat_expand(self, match):
        file_name = match.group(1).strip()

        if file_name in self.cat_cache:
            return self.cat_cache[file_name]

        try:
            with open(file_name) as cat_file:
                content = cat_file.read().replace('\n', ' ').strip()
        except IOError as ex:
            print(ex, file=sys.stderr)
            content = None

        self.cat_cache[file_name] = content
        return content

    def parse_command(self, command, file, description):
        while command:
            first_space = command.find(' ', 1)
            if (first_space == -1):
                first_space = len(command)

            if command[0:first_space].endswith('/clang') or command[0:first_space].endswith('/clang++'):
                break

            command = command[first_space:]

        command = command.strip()

        if not command:
            return False

        if command.endswith("\""):
            command = command[:-1]

        self.compdb.append({
            'directory': self.root_dir,
            # 'command': command,
            'file': file,
            'arguments': parse_arguments(command)
        })
        return True

    def get_build_cmd_for_module(self, module):
        description = ""
        with open(self.ninja_file) as ninja_file:
            for line in ninja_file:
                rule_match = self.RULE_PATTERN.match(line)
                if rule_match:
                    rule_name = rule_match.group(1)
                    continue

                description_match = self.DESCRIPTION_PATTERN.match(line)
                if description_match:
                    description = description_match.group(1)
                    continue

                if module not in description:
                    continue
                
                if module + "_32" in description:
                    continue

                command_match = self.COMMAND_PATTERN.match(line)
                if command_match:
                    self.rules[rule_name] = command_match.group(1)
                    continue

                build_match = self.BUILD_PATTERN.match(line)
                if not build_match:
                    continue

                command = self.rules.get(build_match.group('rule'))
                if not command:
                    continue

                file = build_match.group('file')
                if file.endswith('.S') or file.endswith('.o'):  # Skip asm and .o files
                    continue

                # print("cmd: %s" %(command))
                command = self.CAT_PATTERN.sub(self.cat_expand, command)
                # print("cmd2: %s" %(command))
                has_subcommands = False
                for subcommand in self.SUBCOMMAND_PATTERN.finditer(command):
                    has_subcommands = True
                    # print("subcommand: %s" %(subcommand))
                    if self.parse_command(subcommand.group(1), file, description):
                        break

                if not has_subcommands:
                    self.parse_command(command, file, description)
        return self.compdb

def get_all_makefiles_in_src(path):
    makefile_path_list = []
    for home, dirs, files in os.walk(path):
        for file in files:
            if file.endswith('.mk') or file.endswith('.bp'):
                makefile_path_list.append(os.path.join(home, file))
    return makefile_path_list


def main():
    parser = argparse.ArgumentParser(description='Generate compile_commands.json for all local modules in path')
    parser.add_argument('--version', action='version', version='%(prog)s '+__version__)
    parser.add_argument('--target', help='build target')
    parser.add_argument('--android_root', help='android root dir')
    parser.add_argument('--src', help='path to generate compdb')
    args = parser.parse_args()

    if not args.src:
        logging.error("no src dir")
        exit(-1)
    if not args.target:
        logging.error("no target")
        exit(-2)
    if not args.android_root:
        logging.error("no android root dir")
        exit(-3)


    ninja_file = NinjaFileObj(args.target, args.android_root)

    for make_file_path in get_all_makefiles_in_src(args.src):
        print(make_file_path)
        makefile = MakefileObj(make_file_path)
        local_modules = makefile.get_local_modules()
        for module in local_modules:
            logging.info("get build command for module: %s" %(module))
            ninja_file.get_build_cmd_for_module(module, makefile.makefile_type)

    ninja_file.save_compile_db_file()

# Call main function
if (__name__ == "__main__"):
    ret = 0
    try:
        ret = main()
    except Exception as e:
        logging.error('Unexpected error:' + str(sys.exc_info()[0]))
        logging.exception(e)
    sys.exit(ret)