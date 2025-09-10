/**
 * Secure Scrubber - Interactive CLI (Node.js)
 * -------------------------------------------
 */
const fs = require('fs');
const path = require('path');
const { spawn, exec } = require('child_process');
const inquirer = require('inquirer');
const chalk = require('chalk');

let pythonProcess = null; 

const displayHeader = () => {
  console.clear();
  console.log(chalk.yellow('//////////////////////////////////////////////////'));
  console.log(chalk.yellow('//                                              //'));
  console.log(chalk.yellow('//') + chalk.bold('      D A T A   S C R U B B E R               ') + chalk.yellow('//'));
  console.log(chalk.yellow('//                                              //'));
  console.log(chalk.yellow('//////////////////////////////////////////////////\n'));

  console.log(chalk.red.bold('WARNING: This tool permanently destroys data. Use with extreme caution.'));
};

const getDrives = () => {
  return new Promise((resolve) => {
    const isWindows = process.platform === 'win32';
    const command = isWindows ? 'wmic logicaldisk get caption,volumename,size,freespace' : 'df -h';

    exec(command, (error, stdout) => {
      if (error) {
        resolve([]);
        return;
      }

      const drives = [];
      const lines = stdout.trim().split('\n').slice(1); 

      if (isWindows) {
        lines.forEach(line => {
          const parts = line.trim().split(/\s+/);
          if (parts.length < 2) return;
          const caption = parts[0];
          const freeSpace = parseInt(parts[1], 10);
          const size = parseInt(parts[2], 10);
          const volumeName = parts.slice(3).join(' ') || '[No Name]';

          if (caption && !isNaN(size)) {
            const freeGB = (freeSpace / 1024 / 1024 / 1024).toFixed(1);
            const sizeGB = (size / 1024 / 1024 / 1024).toFixed(1);
            drives.push({
              name: `${caption} ${volumeName} (${freeGB}GB free / ${sizeGB}GB total)`,
              value: `${caption}\\`, // e.g., "C:\"
            });
          }
        });
      } else { // Linux
        lines.forEach(line => {
          const parts = line.trim().split(/\s+/);
          const mountPoint = parts[parts.length - 1];
          if (mountPoint && mountPoint.startsWith('/')) {
             const size = parts[1];
             const used = parts[2];
             const available = parts[3];
             drives.push({
                 name: `${mountPoint} (${available} free / ${size} total)`,
                 value: mountPoint,
             });
          }
        });
      }
      resolve(drives);
    });
  });
};

const startWipeProcess = (targetPath, mode) => {
    console.log(chalk.blue('\nStarting Python wiping engine. This may take a very long time...'));
    console.log(chalk.red.bold('Press CTRL+C at any time to safely cancel the process.\n'));
    console.log(chalk.yellow('Note: System files and protected directories will be automatically skipped.'));
    console.log(chalk.yellow('If you see permission errors, try running as Administrator/sudo.\n'));
    console.log(chalk.gray('You will see live progress updates from the engine below.\n'));

    const pythonExecutable = process.platform === 'win32' ? 'python' : 'python3';
    pythonProcess = spawn(pythonExecutable, [path.join(__dirname, 'main.py'), '--path', targetPath, '--mode', mode]);

    // --- Cancellation Logic ---
    process.stdin.resume();
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (key) => {
        if (key === '\u0003') { // Ctrl+C
            if (pythonProcess) {
                console.log(chalk.yellow.bold('\n\n[INFO] Cancellation signal (CTRL+C) detected. Sending kill signal to engine...'));
                console.log(chalk.yellow.bold('[INFO] The process will stop after its current operation completes.\n'));
                pythonProcess.kill('SIGTERM');
            }
        }
    });

    pythonProcess.stdout.on('data', (data) => {
        const message = data.toString().trim();
        if (message) {
            try {
                // Try to parse as JSON first (for progress updates)
                const jsonData = JSON.parse(message);
                
                if (jsonData.type === 'progress') {
                    // Display formatted progress bar
                    const progressBar = '█'.repeat(Math.floor(jsonData.percentage / 2)) + 
                                       '░'.repeat(50 - Math.floor(jsonData.percentage / 2));
                    console.log(chalk.blue(`[${jsonData.phase}] `) + 
                               chalk.cyan(`${progressBar} `) + 
                               chalk.yellow(`${jsonData.percentage}% `) +
                               chalk.gray(`(${jsonData.speed}, ETA: ${jsonData.eta})`));
                } else if (jsonData.type === 'status') {
                    console.log(chalk.green(`[ENGINE] `) + `${jsonData.message}`);
                } else if (jsonData.type === 'error') {
                    console.error(chalk.red(`[ENGINE_ERROR] `) + `${jsonData.message}`);
                }
            } catch (e) {
                // Not JSON, display as regular message
                console.log(chalk.green(`[ENGINE] `) + `${message}`);
            }
        }
    });

    pythonProcess.stderr.on('data', (data) => {
        const message = data.toString().trim();
        if (message) console.error(chalk.red(`[ENGINE_ERROR] `) + `${message}`);
    });

    pythonProcess.on('close', (code) => {
        if (code === 0) {
            console.log(chalk.green.bold('\n✅ Scrubber process completed successfully!'));
        } else if (code === 130) {
            console.log(chalk.yellow.bold(`\n🟡 Scrubber process was safely cancelled by the user.`));
        } else {
            console.error(chalk.red.bold(`\n❌ Scrubber process failed with exit code ${code}.`));
        }
        console.log('\nPress any key to exit.');
        process.stdin.setRawMode(true);
        process.stdin.resume();
        process.stdin.removeAllListeners('data'); // Remove our Ctrl+C listener
        process.stdin.on('data', process.exit.bind(process, 0));
    });
};

const startWipeSequence = async () => {
    displayHeader();
    console.log(chalk.blue('Scanning for available drives...'));

    const drives = await getDrives();

    if (drives.length === 0) {
        console.log(chalk.red('Could not find any drives. Please ensure you have permissions to view them.'));
        setTimeout(main, 2000);
        return;
    }

    const { targetPath } = await inquirer.prompt([{
        type: 'list',
        name: 'targetPath',
        message: 'Select the drive you want to wipe:',
        choices: [...drives, new inquirer.Separator(), { name: 'Cancel', value: 'cancel' }],
    }]);

    if (targetPath === 'cancel') {
        console.log(chalk.yellow('\nOperation cancelled. Returning to main menu...'));
        setTimeout(main, 1500);
        return;
    }
    
    const resolvedPath = path.resolve(targetPath);
    displayHeader();
    console.log(`Wipe Target: ${chalk.cyan(resolvedPath)}\n`);

    const { wipeMode } = await inquirer.prompt([{
        type: 'list',
        name: 'wipeMode',
        message: 'Select a wiping method:',
        choices: [{
            name: chalk.yellow('Quick Wipe (Fast, Insecure)') + ' - Deletes files without overwriting. Data IS recoverable.',
            value: 'quick',
        }, {
            name: chalk.green('Secure Wipe (Slow, Recommended)') + ' - Overwrites files once, then fills free space.',
            value: 'secure',
        }, {
            name: chalk.red('Paranoid Wipe (Very Slow)') + ' - Overwrites files 3 times, then fills free space.',
            value: 'paranoid',
        },
        new inquirer.Separator(), {
            name: 'Cancel (Back to Main Menu)',
            value: 'cancel',
        }, ],
    }]);

    if (wipeMode === 'cancel') {
        console.log(chalk.yellow('\nOperation cancelled. Returning to main menu...'));
        setTimeout(main, 1500);
        return;
    }

    console.log(chalk.red.bold('\n--- FINAL CONFIRMATION ---'));
    console.log(chalk.white(`You are about to permanently destroy all user-accessible data on the drive:`));
    console.log(chalk.bgRed.white.bold(`  ${resolvedPath}  `));
    console.log(chalk.white(`This action is irreversible.`));

    // For safety, we ask the user to type the drive letter/path, not a random string.
    const confirmationPrompt = process.platform === 'win32'
        ? `To proceed, please type the drive letter (e.g., C:):`
        : `To proceed, please type the full path again:`;
        
    const { confirmation } = await inquirer.prompt([{
        name: 'confirmation',
        message: confirmationPrompt,
    }]);

    // Normalize paths for a reliable comparison
    const normalizedConfirmation = path.normalize(confirmation).replace(/\\$/, '');
    const normalizedTargetPath = path.normalize(resolvedPath).replace(/\\$/, '');

    if (normalizedConfirmation.toLowerCase() === normalizedTargetPath.toLowerCase()) {
        console.log(chalk.green('Confirmation matched. Starting the wipe...'));
        startWipeProcess(resolvedPath, wipeMode);
    } else {
        console.log(chalk.red('Confirmation did not match. Operation cancelled for safety.'));
        setTimeout(main, 2000);
    }
};


const scanDrivesForDisplay = async () => {
  displayHeader();
  console.log(chalk.blue('\nScanning for connected drives...'));
  
  const drives = await getDrives();

  if (drives.length === 0) {
    console.log(chalk.red('Could not find any drives.'));
  } else {
    console.log(chalk.green('Found the following accessible drives:'));
    drives.forEach(drive => console.log(chalk.cyan(`- ${drive.name}`)));
  }

  inquirer.prompt([{
    name: 'continue',
    type: 'input',
    message: '\nPress Enter to return to the main menu...',
  }, ]).then(main);
};

const main = async () => {
    displayHeader();
    const { choice } = await inquirer.prompt([{
        type: 'list',
        name: 'choice',
        message: 'What would you like to do?',
        choices: [
            { name: 'Scan for Connected Drives', value: 'scan' },
            { name: 'Wipe a Drive', value: 'wipe' },
            new inquirer.Separator(),
            { name: 'Exit', value: 'exit' },
        ]
    }]);

    switch (choice) {
        case 'scan':
            await scanDrivesForDisplay();
            break;
        case 'wipe':
            await startWipeSequence();
            break;
        case 'exit':
            console.log(chalk.yellow('\nExiting application.'));
            process.exit(0);
    }
};

main();