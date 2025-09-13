const fs = require('fs');
const path = require('path');
const { spawn, exec } = require('child_process');
const inquirer = require('inquirer');
const chalk = require('chalk');

let pythonProcess = null;
let spinnerInterval = null;

const displayHeader = () => {
    console.clear();
    const boxWidth = 50;
    const title = ' D A T A   S C R U B B E R ';
    const padding = ' '.repeat(Math.floor((boxWidth - title.length) / 2));
    const header = chalk.yellow('+' + '-'.repeat(boxWidth) + '+\n') +
                   chalk.yellow('|' + ' '.repeat(boxWidth) + '|\n') +
                   chalk.yellow('|') + chalk.bold.white(padding + title + padding) + chalk.yellow(' |\n') +
                   chalk.yellow('|' + ' '.repeat(boxWidth) + '|\n') +
                   chalk.yellow('+' + '-'.repeat(boxWidth) + '+\n');
    console.log(header);
    console.log(chalk.bgRed.white.bold(' WARNING: ') + chalk.red(' This tool performs destructive, low-level operations.\n'));
};

// Prefer Python's own drive enumeration for accuracy and consistency
const getDrives = () => {
    return new Promise((resolve) => {
        const isWindows = process.platform === 'win32';
        const pythonExecutable = isWindows ? 'python' : 'python3';
        const mainPy = path.join(__dirname, 'main.py');
        // Argparse in main.py requires -p, -m, -f; pass safe placeholders with --dry-run
        const placeholderPath = isWindows ? 'C:\\' : '/dev/null';
        const placeholderFs = isWindows ? 'NTFS' : 'ext4';
        const cmd = `"${pythonExecutable}" "${mainPy}" -p ${placeholderPath} -m secure -f ${placeholderFs} --list-drives --dry-run`;

        exec(cmd, { windowsHide: true }, (err, stdout) => {
            try {
                if (err) throw err;
                const lines = stdout.trim().split('\n');
                const lastJsonLine = lines.reverse().find(l => {
                    try { const j = JSON.parse(l); return j && j.type === 'drives'; } catch { return false; }
                });
                if (!lastJsonLine) throw new Error('No drives JSON');
                const payload = JSON.parse(lastJsonLine);
                const items = payload.items || [];
                const choices = items.map(d => {
                    const isSys = !!d.is_system;
                    const removable = !!d.is_removable;
                    const totalGB = d.total ? (d.total / 1073741824).toFixed(1) + 'GB' : 'N/A';
                    const fs = d.fstype || 'Unknown';
                    const label = d.label ? ` ${chalk.white(d.label)}` : '';
                    const badge = [
                        removable ? chalk.green('[REMOVABLE]') : chalk.gray('[INTERNAL]'),
                        isSys ? chalk.red('[SYSTEM]') : ''
                    ].filter(Boolean).join(' ');

                    // For Windows, pass the root path (e.g., E:\). For POSIX, pass the block device (e.g., /dev/sdb1)
                    const value = isWindows ? d.path : d.device;
                    const name = `${chalk.cyan(isWindows ? d.path : d.device)}${label} ${chalk.white(`(${fs}, ${totalGB})`)} ${badge}`;
                    const choice = { name, value };
                    if (isSys) choice.disabled = 'System volume (blocked)';
                    return choice;
                });
                if (choices.length > 0) return resolve(choices);
                throw new Error('Empty drive list');
            } catch {
                // Fallback to legacy platform tools
                const fallbackCmd = isWindows ? 'wmic logicaldisk get caption,volumename,size' : "lsblk -o NAME,FSTYPE,SIZE,MOUNTPOINT -b -p -n";
                exec(fallbackCmd, (error, stdout2) => {
                    if (error) return resolve([]);
                    const lines = stdout2.trim().split('\n').filter(Boolean);
                    const drives = [];
                    if (isWindows) {
                        lines.slice(1).forEach(line => {
                            const parts = line.trim().split(/\s{2,}/);
                            if (parts.length < 2) return;
                            const caption = parts[0];
                            const size = parseInt(parts[1], 10);
                            const volumeName = parts.length > 2 ? parts[2] : '[No Name]';
                            if (caption && !isNaN(size)) {
                                const sizeGB = (size / 1073741824).toFixed(1);
                                drives.push({
                                    name: `${chalk.cyan(caption)} ${chalk.white(volumeName)} (${sizeGB}GB)`,
                                    value: caption + '\\',
                                });
                            }
                        });
                    } else {
                        lines.forEach(line => {
                            const parts = line.trim().split(/\s+/);
                            const devName = parts[0];
                            const size = parseInt(parts[2], 10);
                            const mountPoint = parts[3] || '[Not Mounted]';
                            if (devName && !isNaN(size)) {
                                const sizeGB = (size / 1073741824).toFixed(1);
                                drives.push({
                                    name: `${chalk.cyan(devName)} @ ${chalk.white(mountPoint)} (${sizeGB}GB)`,
                                    value: devName,
                                });
                            }
                        });
                    }
                    resolve(drives);
                });
            }
        });
    });
};

const startWipeProcess = (args) => {
    displayHeader();
    const fullArgs = [path.join(__dirname, 'main.py'), ...args];
    const targetPath = args[args.indexOf('--path') + 1];
    const mode = args[args.indexOf('--mode') + 1];

    console.log(chalk.blue('Starting Python wiping engine...\n'));
    console.log(chalk.yellow('Target Device: ') + chalk.white.bold(targetPath));
    console.log(chalk.yellow('Wipe Mode:   ') + chalk.white.bold(mode) + '\n');
    console.log(chalk.red.bold('Press CTRL+C to safely cancel the process.\n'));

    let logMessages = [];
    const logBoxHeight = 10;

    const spinnerFrames = ['-', '\\', '|', '/'];
    let frameIndex = 0;
    let currentPhase = 'Initializing...';

    const renderUI = () => {
        process.stdout.write('\x1B[6;1H'); // Move cursor to row 6, col 1
        process.stdout.write('\x1B[J'); // Clear from cursor to end of screen

        const spinner = chalk.green(spinnerFrames[frameIndex]);
        console.log(`${spinner} ${chalk.bold('Current Phase: ')} ${chalk.cyan(currentPhase)}`);
        console.log(chalk.gray(`\n(This may take several hours. The application is working.)\n`));

        console.log(chalk.yellow('+' + '-'.repeat(72) + '+'));
        const displayedLogs = logMessages.slice(-logBoxHeight);
        for(let i = 0; i < logBoxHeight; i++) {
            let logLine = displayedLogs[i] || '';
            console.log(chalk.yellow('| ') + logLine.padEnd(70) + chalk.yellow(' |'));
        }
        console.log(chalk.yellow('+' + '-'.repeat(72) + '+'));
    };

    const addLog = (message, type = 'status') => {
        const prefix = { error: chalk.red.bold('❌ ERROR'), status: chalk.gray.bold('ℹ️ INFO') }[type] || chalk.gray.bold('LOG');
        const timestamp = new Date().toLocaleTimeString();
        logMessages.push(`${chalk.dim(timestamp)} ${prefix}: ${message}`);
    };

    renderUI();

    const pythonExecutable = process.platform === 'win32' ? 'python' : 'python3';
    pythonProcess = spawn(pythonExecutable, fullArgs, { stdio: ['pipe', 'pipe', 'pipe'] });

    spinnerInterval = setInterval(() => {
        frameIndex = (frameIndex + 1) % spinnerFrames.length;
        renderUI(); // Redraw with latest log and spinner state
    }, 150);

    const processOutput = (data) => {
        data.toString().trim().split('\n').forEach(line => {
            try {
                const jsonData = JSON.parse(line);
                if (jsonData.type === 'progress') {
                     currentPhase = jsonData.phase;
                } else {
                    addLog(jsonData.message, jsonData.type);
                }
            } catch (e) {
                addLog(line, 'raw'); // Log any non-json output from system tools
            }
        });
    };

    pythonProcess.stdout.on('data', processOutput);
    pythonProcess.stderr.on('data', processOutput);

    pythonProcess.on('close', (code) => {
        clearInterval(spinnerInterval);
        displayHeader();
        if (code === 0) console.log(chalk.green.bold('\n✅ Scrubber process completed successfully!'));
        else if (code === 130) console.log(chalk.yellow.bold(`\n🟡 Scrubber process was safely cancelled by the user.`));
        else console.error(chalk.red.bold(`\n❌ Scrubber process failed with exit code ${code}. Review logs for details.`));

        console.log('\n--- Final Log ---');
        logMessages.forEach(log => console.log(log));

        inquirer.prompt([{ name: 'continue', type: 'input', message: '\nPress Enter to return to the main menu...'}]).then(main);
    });
};

const startWipeSequence = async () => {
    displayHeader();
    const drives = await getDrives();
    if (drives.length === 0) {
        console.log(chalk.red('Could not find any drives. Ensure you are running with administrator/root privileges.'));
        return setTimeout(main, 3000);
    }

    const { targetPath } = await inquirer.prompt([{
        type: 'list', name: 'targetPath', message: 'Select the target device to wipe:',
        choices: [...drives, new inquirer.Separator(), { name: 'Cancel', value: 'cancel' }],
    }]);
    if (targetPath === 'cancel') return main();

    const { wipeMode } = await inquirer.prompt([{
        type: 'list', name: 'wipeMode', message: 'Select a wiping method:',
        choices: [
            { name: chalk.green('Secure Wipe') + ' - 4-step: full format, quick reformat, fill free space, final quick format.', value: 'secure' },
            { name: chalk.red('Paranoid Wipe') + ' - Overwrites the entire partition multiple times, then formats.', value: 'paranoid' },
            { name: chalk.yellow('Legacy Fast Wipe') + ' - Destroy metadata (first 512MB), then quick format.', value: 'legacy-fast' },
            new inquirer.Separator(), { name: 'Cancel', value: 'cancel' }
        ],
    }]);
    if (wipeMode === 'cancel') return main();

    // Filesystem is mandatory for all modes in main.py
    const isWindows = process.platform === 'win32';
    const fsChoices = isWindows
        ? [{ name: 'NTFS', value: 'NTFS' }, { name: 'exFAT', value: 'exFAT' }]
        : [{ name: 'ext4', value: 'ext4' }, { name: 'exFAT', value: 'exfat' }, { name: 'VFAT (FAT32)', value: 'vfat' }];
    const { targetFs } = await inquirer.prompt([{
        type: 'list', name: 'targetFs', message: 'Select the NEW filesystem for the drive:',
        choices: [...fsChoices, new inquirer.Separator(), { name: 'Cancel', value: 'cancel' }]
    }]);
    if (targetFs === 'cancel') return main();
    const filesystem = targetFs;

    // Paranoid passes (optional, default 3)
    let passesArg = [];
    if (wipeMode === 'paranoid') {
        const { passes } = await inquirer.prompt([{
            type: 'number', name: 'passes', message: 'Number of overwrite passes (default 3):', default: 3,
            validate: v => Number.isInteger(v) && v >= 1 && v <= 10 ? true : 'Enter an integer between 1 and 10'
        }]);
        passesArg = ['--passes', String(passes)];
    }

    displayHeader();
    console.log(chalk.red.bold('\n--- FINAL CONFIRMATION ---'));
    console.log(chalk.white(`You are about to perform a '${chalk.bold(wipeMode)}' wipe on:`));
    console.log(chalk.bgRed.white.bold(`  ${targetPath}  `));
    if (filesystem) console.log(chalk.white(`The device will be formatted as `) + chalk.bgYellow.black.bold(` ${filesystem} `));
    console.log(chalk.yellow.bold('\nThis action is IRREVERSIBLE. All data will be permanently destroyed.'));

    const { confirmation } = await inquirer.prompt([{
        name: 'confirmation', message: `To proceed, please type the full device path again:`,
    }]);

    const normalizedConfirm = path.normalize(confirmation).trim();
    const normalizedTarget = path.normalize(targetPath).trim();

    if (normalizedConfirm.toLowerCase() === normalizedTarget.toLowerCase()) {
        // On Linux/macOS, require root only when starting the wipe (allow UI & listing without root)
        if (process.platform !== 'win32' && typeof process.getuid === 'function' && process.getuid() !== 0) {
            console.log(chalk.red.bold('Error: Wiping requires root privileges on Linux/macOS.'));
            console.log(chalk.yellow('Please run it with: sudo node index.js'));
            return setTimeout(main, 3000);
        }
        const scriptArgs = ['--path', targetPath, '--mode', wipeMode, '--filesystem', filesystem, ...passesArg];
        startWipeProcess(scriptArgs);
    } else {
        console.log(chalk.red('Confirmation did not match. Operation cancelled for safety.'));
        setTimeout(main, 2000);
    }
};

const main = async () => {
    displayHeader();
    const { choice } = await inquirer.prompt([{
        type: 'list', name: 'choice', message: 'What would you like to do?',
        choices: [
            { name: 'Wipe a Drive / Partition', value: 'wipe' },
            new inquirer.Separator(),
            { name: 'Exit', value: 'exit' },
        ]
    }]);

    if (choice === 'wipe') await startWipeSequence();
    else process.exit(0);
};

main();
