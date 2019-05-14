################################################################################
# Utility classes to hold calibration data for a cryomodule, and Q0 measurement
# data for each of that cryomodule's cavities
# Authors: Lisa Zacarias, Ben Ripman
################################################################################

from __future__ import print_function, division
from csv import writer, reader
from copy import deepcopy
from decimal import Decimal
from os.path import isfile
from sys import stderr
from numpy import mean, exp, log10
from scipy.stats import linregress
from numpy import polyfit
from matplotlib import pyplot as plt
from collections import OrderedDict
from datetime import datetime, timedelta
from abc import abstractproperty, ABCMeta, abstractmethod
from typing import List

from utils import (writeAndFlushStdErr, MYSAMPLER_TIME_INTERVAL, TEST_MODE,
                   VALVE_POSITION_TOLERANCE, HEATER_TOLERANCE, GRAD_TOLERANCE,
                   MIN_RUN_DURATION, getYesNo, get_float_lim, writeAndWait,
                   MAX_DS_LL, cagetPV, caputPV, getTimeParams, MIN_DS_LL,
                   parseRawData, genAxis)


class Container(object):

    def __init__(self, cryModNumSLAC, cryModNumJLAB):
        self.cryModNumSLAC = cryModNumSLAC
        self.cryModNumJLAB = cryModNumJLAB

        self.name = self.addNumToStr("CM{CM}")

        self.dsPressurePV = self.addNumToStr("CPT:CM0{CM}:2302:DS:PRESS")
        self.jtModePV = self.addNumToStr("CPV:CM0{CM}:3001:JT:MODE")
        self.jtPosSetpointPV = self.addNumToStr("CPV:CM0{CM}:3001:JT:POS_SETPT")

        # The double curly braces are to trick it into a partial formatting
        # (CM gets replaced first, and {{INFIX}} -> {INFIX} for later)
        lvlFormatStr = self.addNumToStr("CLL:CM0{CM}:{{INFIX}}:{{LOC}}:LVL")

        self.dsLevelPV = lvlFormatStr.format(INFIX="2301", LOC="DS")
        self.usLevelPV = lvlFormatStr.format(INFIX="2601", LOC="US")

        valveLockFormatter = "CPID:CM0{CM}:3001:JT:CV_{SUFF}"
        self.cvMaxPV = self.addNumToStr(valveLockFormatter, "MAX")
        self.cvMinPV = self.addNumToStr(valveLockFormatter, "MIN")
        self.valvePV = self.addNumToStr(valveLockFormatter, "VALUE")

        self.dataSessions = {}

    # setting this allows me to create abstract methods and parameters, which
    # are basically things that all inheriting classes MUST implement
    __metaclass__ = ABCMeta

    @abstractmethod
    def walkHeaters(self, perHeaterDelta):
        raise NotImplementedError

    @abstractproperty
    def idxFile(self):
        raise NotImplementedError

    @abstractproperty
    def heaterDesPVs(self):
        raise NotImplementedError

    @abstractproperty
    def heaterActPVs(self):
        raise NotImplementedError

    @abstractmethod
    def addDataSessionFromRow(self, row, indices, refHeatLoad,
                              calibSession=None, refGradVal=None):
        return NotImplementedError

    @abstractmethod
    def genDataSession(self, startTime, endTime, timeInt, refValvePos,
                       refHeatLoad, refGradVal=None, calibSession=None):
        return NotImplementedError

    # Returns a list of the PVs used for this container's data acquisition
    @abstractmethod
    def getPVs(self):
        return NotImplementedError

    @abstractmethod
    def hash(self, startTime, endTime, timeInt, slacNum, jlabNum,
             calibSession=None, refGradVal=None):
        return NotImplementedError

    # noinspection PyTupleAssignmentBalance,PyTypeChecker
    def getRefValvePos(self, numHours, checkForFlatness=True):
        # type: (float, bool) -> float

        getNewPos = getYesNo("Determine new JT Valve Position? (May take 2 "
                             "hours) ")

        if not getNewPos:
            desPos = get_float_lim("Desired JT Valve Position: ", 0, 100)
            print("\nDesired JT Valve position is {POS}".format(POS=desPos))
            return desPos

        print("\nDetermining Required JT Valve Position...")

        start = datetime.now() - timedelta(hours=numHours)
        numPoints = int((60 / MYSAMPLER_TIME_INTERVAL) * (numHours * 60))
        signals = [self.dsLevelPV, self.valvePV]

        csvReader = parseRawData(start, numPoints, signals)

        csvReader.next()
        valveVals = []
        llVals = []

        for row in csvReader:
            try:
                valveVals.append(float(row.pop()))
                llVals.append(float(row.pop()))
            except ValueError:
                pass

        # Fit a line to the liquid level over the last [numHours] hours
        m, b, _, _, _ = linregress(range(len(llVals)), llVals)

        # If the LL slope is small enough, return the average JT valve position
        # over the requested time span
        if not checkForFlatness or (checkForFlatness and log10(abs(m)) < 5):
            desPos = round(mean(valveVals), 1)
            print("\nDesired JT Valve position is {POS}".format(POS=desPos))
            return desPos

        # If the LL slope isn't small enough, wait for it to stabilize and then
        # repeat this process (and assume that it's flat enough at that point)
        else:
            print("Need to figure out new JT valve position")

            self.waitForLL()

            writeAndWait("\nWaiting 1 hour 45 minutes for LL to stabilize...")

            start = datetime.now()
            while (datetime.now() - start).total_seconds() < 6300:
                writeAndWait(".", 5)

            return self.getRefValvePos(0.25, False)

    # We consider the cryo situation to be good when the liquid level is high
    # enough and the JT valve is locked in the correct position
    def waitForCryo(self, desPos):
        # type: (float) -> None
        self.waitForLL()
        self.waitForJT(desPos)

    def waitForLL(self):
        # type: () -> None
        writeAndWait("\nWaiting for downstream liquid level to be {LL}%..."
                     .format(LL=MAX_DS_LL))

        while abs(MAX_DS_LL - float(cagetPV(self.dsLevelPV))) > 1:
            writeAndWait(".", 5)

        print("\ndownstream liquid level at required value")

    def waitForJT(self, desPosJT):
        # type: (float) -> None

        writeAndWait("\nWaiting for JT Valve to be locked at {POS}..."
                     .format(POS=desPosJT))

        mode = cagetPV(self.jtModePV)

        # One way for the JT valve to be locked in the correct position is for
        # it to be in manual mode and at the desired value
        if mode == "0":
            while float(cagetPV(self.jtPosSetpointPV)) != desPosJT:
                writeAndWait(".", 5)

        # Another way for the JT valve to be locked in the correct position is
        # for it to be automatically regulating and have the upper and lower
        # regulation limits be set to the desired value
        else:

            while float(cagetPV(self.cvMinPV)) != desPosJT:
                writeAndWait(".", 5)

            while float(cagetPV(self.cvMaxPV)) != desPosJT:
                writeAndWait(".", 5)

        print("\nJT Valve locked")

    def addNumToStr(self, formatStr, suffix=None):
        if suffix:
            return formatStr.format(CM=self.cryModNumJLAB, SUFF=suffix)
        else:
            return formatStr.format(CM=self.cryModNumJLAB)

    def addDataSession(self, startTime, endTime, timeInt, refValvePos,
                       refHeatLoad=None, refGradVal=None, calibSession=None):
        # type: (datetime, datetime, int, float, float, float, DataSession) -> DataSession

        # Determine the current electric heat load on the cryomodule (the sum
        # of all the heater act values). This will only ever be None when we're
        # taking new data
        if not refHeatLoad:
            refHeatLoad = 0
            for heaterActPV in self.heaterActPVs:
                refHeatLoad += float(cagetPV(heaterActPV))

        sessionHash = self.hash(startTime, endTime, timeInt,
                                self.cryModNumSLAC, self.cryModNumJLAB,
                                calibSession, refGradVal)

        # Only create a new calibration data session if one doesn't already
        # exist with those exact parameters
        if sessionHash not in self.dataSessions:
            session = self.genDataSession(startTime, endTime, timeInt,
                                          refValvePos, refHeatLoad, refGradVal,
                                          calibSession)
            self.dataSessions[sessionHash] = session

        return self.dataSessions[sessionHash]


class Cryomodule(Container):

    def __init__(self, cryModNumSLAC, cryModNumJLAB):
        super(Cryomodule, self).__init__(cryModNumSLAC, cryModNumJLAB)

        # Give each cryomodule 8 cavities
        cavities = {}

        self._heaterDesPVs = []
        self._heaterActPVs = []

        heaterDesStr = self.addNumToStr("CHTR:CM0{CM}:1{{CAV}}55:HV:{SUFF}",
                                        "POWER_SETPT")
        heaterActStr = self.addNumToStr("CHTR:CM0{CM}:1{{CAV}}55:HV:{SUFF}",
                                        "POWER")

        for i in range(1, 9):
            cavities[i] = Cavity(cryMod=self, cavNumber=i)
            self._heaterDesPVs.append(heaterDesStr.format(CAV=i))
            self._heaterActPVs.append(heaterActStr.format(CAV=i))

        # Using an ordered dictionary so that when we generate the report
        # down the line (iterating over the cavities in a cryomodule), we
        # print the results in order (basic dictionaries aren't guaranteed to
        # be ordered)
        self.cavities = OrderedDict(sorted(cavities.items()))

    def hash(self, startTime, endTime, timeInt, slacNum, jlabNum,
             calibSession=None, refGradVal=None):
        return DataSession.hash(startTime, endTime, timeInt, slacNum, jlabNum)

    # calibSession and refGradVal are unused here, they're just there to match
    # the signature of the overloading method in Cavity (which is why they're in
    # the signature for Container - could probably figure out a way around this)
    def addDataSessionFromRow(self, row, indices, refHeatLoad,
                              calibSession=None, refGradVal=None):
        # type: (List[str], dict, float, DataSession, float) -> DataSession

        startTime, endTime, timeInterval = getTimeParams(row, indices)

        # refHeatLoad = float(row[indices["refHeatIdx"]])

        return self.addDataSession(startTime, endTime, timeInterval,
                                   float(row[indices["jtIdx"]]),
                                   refHeatLoad)

    def getPVs(self):
        return ([self.valvePV, self.dsLevelPV, self.usLevelPV]
                + self.heaterDesPVs + self.heaterActPVs)

    def walkHeaters(self, perHeaterDelta):
        # type: (int) -> None

        # negative if we're decrementing heat
        step = 1 if perHeaterDelta > 0 else -1

        for _ in range(abs(perHeaterDelta)):
            for heaterSetpointPV in self.heaterDesPVs:
                currVal = float(cagetPV(heaterSetpointPV))
                caputPV(heaterSetpointPV, str(currVal + step))
                writeAndWait("\nWaiting 30s for cryo to stabilize...\n", 30)

    def genDataSession(self, startTime, endTime, timeInt, refValvePos,
                       refHeatLoad, refGradVal=None, calibSession=None):
        return DataSession(self, startTime, endTime, timeInt, refValvePos,
                           refHeatLoad)

    @property
    def idxFile(self):
        return ("calibrations/calibrationsCM{CM}.csv"
                .format(CM=self.cryModNumSLAC))

    @property
    def heaterDesPVs(self):
        return self._heaterDesPVs

    @property
    def heaterActPVs(self):
        return self._heaterActPVs


class Cavity(Container):
    def __init__(self, cryMod, cavNumber):
        # type: (Cryomodule, int) -> None

        super(Cavity, self).__init__(cryMod.cryModNumSLAC, cryMod.cryModNumJLAB)
        self.parent = cryMod

        self.name = "Cavity {cavNum}".format(cavNum=cavNumber)
        self.cavNum = cavNumber

    # refGradVal and calibSession are required parameters but are nullable to
    # match the signature in Container
    def genDataSession(self, startTime, endTime, timeInt, refValvePos,
                       refHeatLoad, refGradVal=None, calibSession=None):
        return Q0DataSession(self, startTime, endTime, timeInt, refValvePos,
                             refHeatLoad, refGradVal, calibSession)

    def hash(self, startTime, endTime, timeInt, slacNum, jlabNum,
             calibSession=None, refGradVal=None):
        return Q0DataSession.hash(startTime, endTime, timeInt, slacNum, jlabNum,
                                  calibSession, refGradVal)

    def walkHeaters(self, perHeaterDelta):
        return self.parent.walkHeaters(perHeaterDelta)

    # calibSession and refGradVal are required parameters for Cavity data
    # sessions, but they're nullable to match the signature in Container
    def addDataSessionFromRow(self, row, indices, refHeatLoad,
                              calibSession=None, refGradVal=None):
        # type: (List[str], dict, float, DataSession, float) -> Q0DataSession

        startTime, endTime, timeInterval = getTimeParams(row, indices)

        return self.addDataSession(startTime, endTime, timeInterval,
                                   float(row[indices["jtIdx"]]), refHeatLoad,
                                   refGradVal, calibSession)

    def genPV(self, formatStr, suffix):
        return formatStr.format(CM=self.cryModNumJLAB, CAV=self.cavNum,
                                SUFF=suffix)

    def genAcclPV(self, suffix):
        return self.genPV("ACCL:L1B:0{CM}{CAV}0:{SUFF}", suffix)

    def getPVs(self):
        return ([self.parent.valvePV, self.parent.dsLevelPV,
                 self.parent.usLevelPV, self.gradPV,
                 self.parent.dsPressurePV] + self.parent.heaterDesPVs
                + self.parent.heaterActPVs)

    @property
    def idxFile(self):
        return ("q0Measurements/q0MeasurementsCM{CM}.csv"
                .format(CM=self.parent.cryModNumSLAC))

    @property
    def heaterDesPVs(self):
        return self.parent.heaterDesPVs

    @property
    def heaterActPVs(self):
        return self.parent.heaterActPVs

    @property
    def gradPV(self):
        return self.genAcclPV("GACT")


class DataSession(object):

    def __init__(self, container, startTime, endTime, timeInt, refValvePos,
                 refHeatLoad):
        # type: (Container, datetime, datetime, int, float, float) -> None
        self.container = container

        # e.g. calib_CM12_2019-02-25--11-25_18672.csv
        self.fileNameFormatter = "data/calib/cm{CM}/calib_{cryoMod}{suff}"

        self._dataFileName = None
        self._numPoints = None
        self.refValvePos = refValvePos
        self.refHeatLoad = refHeatLoad
        self.timeInt = timeInt
        self.startTime = startTime
        self.endTime = endTime

        self.unixTimeBuff = []
        self.timeBuff = []
        self.valvePosBuff = []
        self.dsLevelBuff = []
        self.gradBuff = []
        self.dsPressBuff = []
        self.elecHeatDesBuff = []
        self.elecHeatActBuff = []

        self.pvBuffMap = {container.valvePV: self.valvePosBuff,
                          container.dsLevelPV: self.dsLevelBuff}

        self.calibSlope = None

        # If we choose the JT valve position correctly, the calibration curve
        # should intersect the origin (0 heat load should translate to 0
        # dLL/dt). The heat adjustment will be equal to the negative x
        # intercept.
        self.heatAdjustment = 0

        # the plot of the raw calibration LL data
        self.liquidVsTimeAxis = None

        # the dLL/dt vs heat load plot with trend line (back-calculated points
        # for cavity Q0 sessions are added later)
        self.heaterCalibAxis = None

        self.dataRuns = []  # type: List[DataRun]

    def __hash__(self):
        return self.hash(self.startTime, self.endTime, self.timeInt,
                         self.container.cryModNumSLAC,
                         self.container.cryModNumJLAB)

    def __str__(self):
        return ("{START} to {END} ({RATE}s sample interval)"
                .format(START=self.startTime, END=self.endTime,
                        RATE=self.timeInt))

    @property
    def runSlopes(self):
        return [run.slope for run in self.dataRuns]

    @property
    def runElecHeatLoads(self):
        return [run.elecHeatLoad for run in self.dataRuns]

    def addRun(self, startIdx, endIdx):
        self.dataRuns.append(DataRun(startIdx, endIdx, self,
                                     len(self.dataRuns) + 1))

    ############################################################################
    # A hash is effectively a unique numerical identifier. The purpose of a
    # hash function is to generate an ID for an object. In this case, we
    # consider data sessions to be identical if they have the same start & end
    # times, mySampler time interval, and cryomodule numbers. This function
    # takes all of those parameters and XORs (the ^ symbol) them.
    #
    # What is an XOR? It's an operator that takes two bit strings and goes
    # through them, bit by bit, returning True (1) only if one bit is 0 and the
    # other is 1
    #
    # EX) consider the following two bit strings a, b, and c = a^b:
    #       a: 101010010010 (2706 in base 10)
    #       b: 100010101011 (2219)
    #       ---------------
    #       c: 001000111001 (569)
    #
    # What we're doing here is taking each input data object's built-in hash
    # function (which returns an int) and XORing those ints together. It's not
    # QUITE unique, but XOR is the accepted way to hash in Python because
    # collisions are extremely rare (especially considering how many inputs we
    # have)
    #
    # As to WHY we're doing this, it's to have an easy way to compare
    # two data sessions so that we can avoid creating (and storing) duplicate
    # data sessions in the Container
    ############################################################################
    @staticmethod
    def hash(startTime, endTime, timeInt, slacNum, jlabNum, calibSession=None,
             refGradVal=None):
        return (hash(startTime) ^ hash(endTime) ^ hash(timeInt) ^ hash(slacNum)
                ^ hash(jlabNum))

    @property
    def numPoints(self):
        if not self._numPoints:
            self._numPoints = int((self.endTime
                                   - self.startTime).total_seconds()
                                  / self.timeInt)
        return self._numPoints

    @property
    def fileName(self):
        if not self._dataFileName:
            # Define a file name for the CSV we're saving. There are calibration
            # files and q0 measurement files. Both include a time stamp in the
            # format year-month-day--hour-minute. They also indicate the number
            # of data points.
            suffixStr = "{start}{nPoints}.csv"
            suffix = suffixStr.format(
                start=self.startTime.strftime("_%Y-%m-%d--%H-%M_"),
                nPoints=self.numPoints)
            cryoModStr = "CM{cryMod}".format(
                cryMod=self.container.cryModNumSLAC)

            self._dataFileName = self.fileNameFormatter.format(
                cryoMod=cryoModStr, suff=suffix,
                CM=self.container.cryModNumSLAC)

        return self._dataFileName

    def processData(self):

        self.parseDataFromCSV()
        self.populateRuns()

        if not self.dataRuns:
            print("{name} has no runs to process and plot."
                  .format(name=self.container.name))
            return

        self.adjustForSettle()
        self.processRuns()
        self.plotAndFitData()

    # takes three related arrays, plots them, and fits some trend lines
    def plotAndFitData(self):
        # TODO improve plots with human-readable time axis

        plt.rcParams.update({'legend.fontsize': 'small'})

        suffix = " ({name} Heater Calibration)".format(name=self.container.name)

        self.liquidVsTimeAxis = genAxis("Liquid Level vs. Time" + suffix,
                                        "Unix Time (s)",
                                        "Downstream Liquid Level (%)")

        for run in self.dataRuns:  # type: DataRun
            # First we plot the actual run data
            self.liquidVsTimeAxis.plot(run.times, run.data, label=run.label)

            # Then we plot the linear fit to the run data
            self.liquidVsTimeAxis.plot(run.times, [run.slope * x + run.intercept
                                                   for x in run.times])

        self.liquidVsTimeAxis.legend(loc='best')
        self.heaterCalibAxis = genAxis("Liquid Level Rate of Change vs."
                                       " Heat Load", "Heat Load (W)",
                                       "dLL/dt (%/s)")

        self.heaterCalibAxis.plot(self.runElecHeatLoads, self.runSlopes,
                                  marker="o", linestyle="None",
                                  label="Heater Calibration Data")

        slopeStr = '{:.2e}'.format(Decimal(self.calibSlope))
        labelStr = "Calibration Fit:  {slope} %/(s*W)".format(slope=slopeStr)

        self.heaterCalibAxis.plot(self.runElecHeatLoads,
                                  [self.calibSlope * x
                                   for x in self.runElecHeatLoads],
                                  label=labelStr)

        self.heaterCalibAxis.legend(loc='best')

    # processRuns iterates over this session's data runs, plots them, and fits
    # trend lines to them
    # noinspection PyTupleAssignmentBalance
    def processRuns(self):

        for run in self.dataRuns:
            run.slope, run.intercept, r_val, p_val, std_err = linregress(
                run.times, run.data)

            # Print R^2 to diagnose whether or not we had a long enough data run
            print("Run {NUM} R^2: ".format(NUM=run.num) + str(r_val ** 2))

        # We're dealing with a cryomodule here so we need to calculate the
        # fit for the heater calibration curve.
        self.calibSlope, yIntercept = polyfit(self.runElecHeatLoads,
                                              self.runSlopes, 1)

        xIntercept = -yIntercept / self.calibSlope

        self.heatAdjustment = -xIntercept
        print("Calibration curve intercept adjust = {ADJUST} W"
              .format(ADJUST=self.heatAdjustment))

        if TEST_MODE:
            for i, run in enumerate(self.dataRuns):
                startTime = self.unixTimeBuff[run.startIdx]
                endTime = self.unixTimeBuff[run.endIdx]
                runStr = "Duration of run {runNum}: {duration}"
                print(runStr.format(runNum=(i + 1),
                                    duration=((endTime - startTime) / 60.0)))

    def getTotalHeatDelta(self, startIdx, currIdx):
        # type: (int, int) -> float
        if currIdx == 0:
            return self.elecHeatDesBuff[startIdx] - self.refHeatLoad

        else:

            prevStartIdx = self.dataRuns[currIdx - 1].startIdx

            elecHeatDelta = (self.elecHeatDesBuff[startIdx]
                             - self.elecHeatDesBuff[prevStartIdx])

            return abs(elecHeatDelta)

    ############################################################################
    # adjustForSettle cuts off data that's corrupted because the heat load on
    # the 2 K helium bath is changing. (When the cavity heater setting or the RF
    # gradient change, it takes time for that change to become visible to the
    # helium because there are intermediate structures with heat capacity.)
    ############################################################################
    def adjustForSettle(self):

        for i, run in enumerate(self.dataRuns):

            startIdx = run.startIdx

            totalHeatDelta = self.getTotalHeatDelta(startIdx, i)

            # Calculate the number of data points to be chopped off the
            # beginning of the data run based on the expected change in the
            # cryomodule heat load. The scale factor is derived from the
            # assumption that a 1 W change in the heat load leads to about 25
            # useless seconds (and that this scales linearly with the change in
            # heat load, which isn't really true).
            cutoff = int(totalHeatDelta * 25)

            idx = self.dataRuns[i].startIdx
            startTime = self.unixTimeBuff[idx]
            duration = 0

            while duration < cutoff:
                idx += 1
                duration = self.unixTimeBuff[idx] - startTime

            self.dataRuns[i].startIdx = idx

            if TEST_MODE:
                print("cutoff: " + str(cutoff))

    # generates a CSV data file (with the raw data from this data session) if
    # one doesn't already exist
    def generateCSV(self):

        def populateHeaterCols(pvList, buff):
            # type: (List[str], List[float]) -> None
            for heaterPV in pvList:
                buff.append(header.index(heaterPV))

        if isfile(self.fileName):
            return self.fileName

        csvReader = parseRawData(self.startTime, self.numPoints,
                                 self.container.getPVs(), self.timeInt)

        if not csvReader:
            return None

        else:

            # TODO test new file generation to see if deepcopy was necessary
            header = csvReader.next()

            heaterDesCols = []
            # TODO not tested yet, so not deleting old code
            populateHeaterCols(self.container.heaterDesPVs, heaterDesCols)
            # for heaterPV in self.container.heaterDesPVs:
            #     heaterDesCols.append(header.index(heaterPV))

            heaterActCols = []
            populateHeaterCols(self.container.heaterActPVs, heaterDesCols)
            # for heaterActPV in self.container.heaterActPVs:
            #     heaterActCols.append(header.index(heaterActPV))

            # So that we don't corrupt the indices while we're deleting them
            colsToDelete = sorted(heaterDesCols + heaterActCols, reverse=True)

            for index in colsToDelete:
                del header[index]

            header.append("Electric Heat Load Setpoint")
            header.append("Electric Heat Load Readback")

            # We're collapsing the readback for each cavity's desired and actual
            # electric heat load into two sum columns (instead of 16 individual
            # columns)
            with open(self.fileName, 'wb') as f:
                csvWriter = writer(f, delimiter=',')
                csvWriter.writerow(header)

                for row in csvReader:
                    # trimmedRow = deepcopy(row)

                    heatLoadSetpoint = 0

                    for col in heaterDesCols:
                        try:
                            heatLoadSetpoint += float(row[col])
                        except ValueError:
                            heatLoadSetpoint = None
                            break

                    heatLoadAct = 0

                    for col in heaterActCols:
                        try:
                            heatLoadAct += float(row[col])
                        except ValueError:
                            heatLoadAct = None
                            break

                    for index in colsToDelete:
                        del row[index]

                    row.append(str(heatLoadSetpoint))
                    row.append(str(heatLoadAct))
                    csvWriter.writerow(row)

            return self.fileName

    # parses CSV data to populate the given session's data buffers
    def parseDataFromCSV(self):
        def linkBuffToColumn(column, dataBuff, headerRow):
            try:
                columnDict[column] = {"idx": headerRow.index(column),
                                      "buffer": dataBuff}
            except ValueError:
                writeAndFlushStdErr("Column " + column + " not found in CSV\n")

        columnDict = {}

        with open(self.fileName) as csvFile:

            csvReader = reader(csvFile)
            header = csvReader.next()

            # Figures out the CSV column that has that PV's data and maps it
            for pv, dataBuffer in self.pvBuffMap.items():
                linkBuffToColumn(pv, dataBuffer, header)

            linkBuffToColumn("Electric Heat Load Setpoint",
                             self.elecHeatDesBuff, header)

            linkBuffToColumn("Electric Heat Load Readback",
                             self.elecHeatActBuff, header)

            try:
                # Data fetched from the JLab archiver has the timestamp column
                # labeled "Date"
                timeIdx = header.index("Date")
                datetimeFormatStr = "%Y-%m-%d-%H:%M:%S"

            except ValueError:
                # Data exported from MyaPlot has the timestamp column labeled
                # "time"
                timeIdx = header.index("time")
                datetimeFormatStr = "%Y-%m-%d %H:%M:%S"

            timeZero = datetime.utcfromtimestamp(0)

            for row in csvReader:
                dt = datetime.strptime(row[timeIdx], datetimeFormatStr)

                self.timeBuff.append(dt)

                # We use the Unix time to make the math easier during data
                # processing
                self.unixTimeBuff.append((dt - timeZero).total_seconds())

                # Actually parsing the CSV data into the buffers
                for col, idxBuffDict in columnDict.items():
                    try:
                        idxBuffDict["buffer"].append(
                            float(row[idxBuffDict["idx"]]))
                    except ValueError:
                        writeAndFlushStdErr("Could not fill buffer: " + str(col)
                                            + "\n")
                        idxBuffDict["buffer"].append(None)

    def isEndOfCalibRun(self, idx, elecHeatLoad):
        # Find inflection points for the desired heater setting
        prevElecHeatLoad = (self.elecHeatDesBuff[idx - 1]
                            if idx > 0 else elecHeatLoad)

        heaterChanged = (elecHeatLoad != prevElecHeatLoad)
        liqLevelTooLow = (self.dsLevelBuff[idx] < MIN_DS_LL)
        valveOutsideTol = (abs(self.valvePosBuff[idx] - self.refValvePos)
                           > VALVE_POSITION_TOLERANCE)
        isLastElement = (idx == len(self.elecHeatDesBuff) - 1)

        heatersOutsideTol = (abs(elecHeatLoad - self.elecHeatActBuff[idx])
                             >= HEATER_TOLERANCE)

        # A "break" condition defining the end of a run if the desired heater
        # value changed, or if the upstream liquid level dipped below the
        # minimum, or if the valve position moved outside the tolerance, or if
        # we reached the end (which is a kinda jank way of "flushing" the last
        # run)
        return (heaterChanged or liqLevelTooLow or valveOutsideTol
                or isLastElement or heatersOutsideTol)

    def checkAndFlushRun(self, isEndOfRun, idx, runStartIdx):
        if isEndOfRun:
            runDuration = (self.unixTimeBuff[idx]
                           - self.unixTimeBuff[runStartIdx])

            if runDuration >= MIN_RUN_DURATION:
                self.addRun(runStartIdx, idx - 1)

            return idx

        return runStartIdx

    # takes the data in an session's buffers and slices it into data "runs"
    # based on cavity heater settings
    def populateRuns(self):
        runStartIdx = 0

        for idx, elecHeatLoad in enumerate(self.elecHeatDesBuff):
            runStartIdx = self.checkAndFlushRun(
                self.isEndOfCalibRun(idx, elecHeatLoad), idx, runStartIdx)


# There is no CalibDataSession class because DataSession has everything that
# is needed for a calibration. There *is* a Q0DataSession class because
# everything inside of DataSession is necessary but not sufficient for cavity
# Q0 measurements.
class Q0DataSession(DataSession):

    def __init__(self, container, startTime, endTime, timeInt, refValvePos,
                 refHeatLoad, refGradVal, calibSession):
        # type: (Cavity, datetime, datetime, int, float, float, float, DataSession) -> None
        super(Q0DataSession, self).__init__(container, startTime, endTime,
                                            timeInt, refValvePos, refHeatLoad)
        self.fileNameFormatter = ("data/q0meas/cm{CM}"
                                  "/q0meas_{cryoMod}_cav{cavityNum}{suff}")
        self.pvBuffMap = {container.parent.valvePV: self.valvePosBuff,
                          container.parent.dsLevelPV: self.dsLevelBuff,
                          container.gradPV: self.gradBuff,
                          container.parent.dsPressurePV: self.dsPressBuff}
        self.refGradVal = refGradVal
        self.calibSession = calibSession

        # Overloading these unnecessarily to give the IDE type hints
        self.container = container
        self.dataRuns = []  # type: List[Q0DataRun]

    def __hash__(self):
        return self.hash(self.startTime, self.endTime, self.timeInt,
                         self.container.cryModNumSLAC,
                         self.container.cryModNumJLAB, self.calibSession,
                         self.refGradVal)

    def plotAndFitData(self):
        # TODO improve plots with human-readable time axis

        plt.rcParams.update({'legend.fontsize': 'small'})

        suffix = " ({name})".format(name=self.container.name)

        self.liquidVsTimeAxis = genAxis("Liquid Level vs. Time" + suffix,
                                        "Unix Time (s)",
                                        "Downstream Liquid Level (%)")

        for run in self.dataRuns:
            # First we plot the actual run data
            self.liquidVsTimeAxis.plot(run.times, run.data, label=run.label)

            # Then we plot the linear fit to the run data
            self.liquidVsTimeAxis.plot(run.times, [run.slope * x + run.intercept
                                                   for x in run.times])

        self.liquidVsTimeAxis.legend(loc='best')

    # noinspection PyTupleAssignmentBalance
    def processRuns(self):

        for run in self.dataRuns:
            run.slope, run.intercept, r_val, p_val, std_err = linregress(
                run.times, run.data)

            # Print R^2 to diagnose whether or not we had a long enough data run
            print("Run {NUM} R^2: ".format(NUM=run.num) + str(r_val ** 2))

        if TEST_MODE:
            for i, run in enumerate(self.dataRuns):
                startTime = self.unixTimeBuff[run.startIdx]
                endTime = self.unixTimeBuff[run.endIdx]
                runStr = "Duration of run {runNum}: {duration}"
                print(runStr.format(runNum=(i + 1),
                                    duration=((endTime - startTime) / 60.0)))

    def printReport(self):
        for run in self.dataRuns:
            run.printReport()

    def populateRuns(self):

        runStartIdx = 0

        for idx, elecHeatLoad in enumerate(self.elecHeatDesBuff):

            try:
                gradChanged = (abs(self.gradBuff[idx] - self.gradBuff[idx - 1])
                               > GRAD_TOLERANCE) if idx != 0 else False
            except TypeError:
                gradChanged = False

            isEndOfQ0Run = (self.isEndOfCalibRun(idx, elecHeatLoad)
                            or gradChanged)

            runStartIdx = self.checkAndFlushRun(isEndOfQ0Run, idx, runStartIdx)

    # Approximates the expected heat load on a cavity from its RF gradient. A
    # cavity with the design Q of 2.7E10 should produce about 9.6 W of heat with
    # a gradient of 16 MV/m. The heat scales quadratically with the gradient. We
    # don't know the correct Q yet when we call this function so we assume the
    # design values.
    @staticmethod
    def approxHeatFromGrad(grad):
        # Gradients < 0 are non-physical so assume no heat load in that case.
        # The gradient values we're working with are readbacks from cavity
        # gradient PVs so it's possible that they could go negative.
        return ((grad / 16) ** 2) * 9.6 if grad > 0 else 0

    @staticmethod
    def hash(startTime, endTime, timeInt, slacNum, jlabNum, calibSession=None,
             refGradVal=None):
        return (hash(startTime) ^ hash(endTime) ^ hash(timeInt) ^ hash(slacNum)
                ^ hash(jlabNum) ^ hash(calibSession) ^ hash(refGradVal))

    # this handles the case where we have multiple heater runs, though our q0
    # measurement automation currently only does one
    @property
    def aveHeatAdjustment(self):
        adjustments = []

        for run in self.dataRuns:
            runAdjustment = run.heatAdjustment
            if runAdjustment:
                adjustments.append(runAdjustment)

        return mean(adjustments) if adjustments else 0

    @property
    def adjustedRunSlopes(self):
        m = self.calibSession.calibSlope
        return [(m * run.totalHeatLoad) for run in self.dataRuns
                if run.isNotHeaterRun]

    @property
    def runHeatLoads(self):
        return [run.totalHeatLoad for run in self.dataRuns
                if run.isNotHeaterRun]



    def getTotalHeatDelta(self, startIdx, currIdx):
        if currIdx == 0:
            totalHeatDelta = (self.elecHeatDesBuff[startIdx] - self.refHeatLoad)
            totalHeatDelta += self.approxHeatFromGrad(self.gradBuff[startIdx])
            return totalHeatDelta

        else:

            prevStartIdx = self.dataRuns[currIdx - 1].startIdx

            elecHeatDelta = (self.elecHeatDesBuff[startIdx]
                             - self.elecHeatDesBuff[prevStartIdx])

            currGrad = self.gradBuff[startIdx]
            currGradHeatLoad = self.approxHeatFromGrad(currGrad)

            prevGrad = self.gradBuff[prevStartIdx]
            prevGradHeatLoad = self.approxHeatFromGrad(prevGrad)

            gradHeatDelta = currGradHeatLoad - prevGradHeatLoad
            return abs(elecHeatDelta + gradHeatDelta)

    @property
    def fileName(self):
        if not self._dataFileName:
            suffixStr = "{start}{nPoints}.csv"
            suffix = suffixStr.format(
                start=self.startTime.strftime("_%Y-%m-%d--%H-%M_"),
                nPoints=self.numPoints)
            cryoModStr = "CM{cryMod}".format(
                cryMod=self.container.cryModNumSLAC)

            self._dataFileName = self.fileNameFormatter.format(
                cryoMod=cryoModStr, suff=suffix,
                cavityNum=self.container.cavNum,
                CM=self.container.cryModNumSLAC)

        return self._dataFileName

    def addRun(self, startIdx, endIdx):
        self.dataRuns.append(Q0DataRun(startIdx, endIdx, self,
                                       len(self.dataRuns) + 1))


class DataRun(object):
    # __metaclass__ = ABCMeta

    def __init__(self, runStartIdx=None, runEndIdx=None, container=None,
                 num=None):
        # type: (int, int, DataSession, int) -> None
        # startIdx and endIdx define the beginning and the end of this data run
        # within the cryomodule or cavity's data buffers
        self.startIdx = runStartIdx
        self.endIdx = runEndIdx

        # All data runs have liquid level information which gets fitted with a
        # line (giving us dLL/dt). The slope and intercept parametrize the line.
        self.slope = None
        self.intercept = None

        self.dataSession = container
        self.num = num

    @property
    def data(self):
        return self.dataSession.dsLevelBuff[self.startIdx:self.endIdx]

    @property
    def times(self):
        return self.dataSession.unixTimeBuff[self.startIdx:self.endIdx]

    # elecHeatLoad is the electric heat load over baseline for this run
    @property
    def elecHeatLoad(self):
        return ((self.dataSession.elecHeatActBuff[self.endIdx]
                - self.dataSession.elecHeatActBuff[0])
                + self.dataSession.heatAdjustment)

    @property
    def elecHeatLoadDes(self):
        return (self.dataSession.elecHeatDesBuff[self.endIdx]
                - self.dataSession.refHeatLoad)

    @property
    def label(self):
        labelStr = "{slope} %/s @ {heatLoad} W Electric Load"

        return labelStr.format(slope='%.2E' % Decimal(self.slope),
                               heatLoad=round(self.elecHeatLoad, 2))

    @property
    def isNotHeaterRun(self):
        return self.elecHeatLoadDes == 0


# Q0DataRun stores all the information about cavity Q0 measurement runs that
# isn't included in the parent class DataRun
class Q0DataRun(DataRun):

    def __init__(self, runStartIdx, runEndIdx, dataSession, num):
        # type: (int, int, Q0DataSession, int) -> None
        super(Q0DataRun, self).__init__(runStartIdx, runEndIdx, dataSession,
                                        num)

        # The average gradient
        self.grad = None
        self._calculatedQ0 = None
        # jank ass thing to tell the IDE that it's a Q0DataSession
        self.dataSession = dataSession

    # Q0 measurement runs have a total heat load value which we calculate
    # by projecting the run's dLL/dt on the cryomodule's heater calibration
    # curve
    @property
    def totalHeatLoad(self):
        if self.isNotHeaterRun:
            return ((self.slope / self.dataSession.calibSession.calibSlope)
                    + self.dataSession.aveHeatAdjustment)
        else:
            return self.elecHeatLoad

    # The RF heat load is equal to the total heat load minus the electric
    # heat load
    @property
    def rfHeatLoad(self):
        if self.elecHeatLoadDes != 0:
            return 0
        else:
            return self.totalHeatLoad - self.elecHeatLoad

    @property
    def heatAdjustment(self):
        if self.elecHeatLoadDes != 0:
            calcHeatLoad = (self.slope
                            / self.dataSession.calibSession.calibSlope)
            return self.elecHeatLoad - calcHeatLoad
        else:
            return None

    # The calculated Q0 value for this run
    # Magical formula from Mike Drury (drury@jlab.org) to calculate Q0 from the
    # measured heat load on a cavity, the RF gradient used during the test, and
    # the pressure of the incoming 2 K helium.
    @property
    def q0(self):
        if self.elecHeatLoadDes != 0:
            return None

        if not self._calculatedQ0:
            q0s = []
            numInvalidGrads = self.dataSession.gradBuff.count(0)

            for idx in range(self.startIdx, self.endIdx):
                archiveGrad = self.dataSession.gradBuff[idx]

                q0s.append(self.calcQ0(archiveGrad if archiveGrad
                                       else self.dataSession.refGradVal,
                                       self.rfHeatLoad,
                                       self.dataSession.dsPressBuff[idx]))

            if numInvalidGrads:
                writeAndFlushStdErr("\nGradient buffer had {NUM} invalid points"
                                    " (used reference gradient value instead) "
                                    "- Consider refetching the data from the "
                                    "archiver\n"
                                    .format(NUM=numInvalidGrads))
                stderr.flush()

            self._calculatedQ0 = mean(q0s)

        return self._calculatedQ0

    @property
    def label(self):
        # This is a heater run. It could be part of a cryomodule heater
        # calibration or it could be part of a cavity Q0 measurement.
        if self.elecHeatLoadDes != 0:
            return super(Q0DataRun, self).label

        # This is an RF run taken during a cavity Q0 measurement.
        else:

            labelStr = "{slope} %/s @ {grad} MV/m\nCalculated Q0: {Q0}"
            q0Str = '{:.2e}'.format(Decimal(self.q0))

            return labelStr.format(slope='%.2E' % Decimal(self.slope),
                                   grad=self.dataSession.refGradVal, Q0=q0Str)

    def printReport(self):
        reportStr = ("\n{cavName} run {runNum} total heat load: {TOT} W\n"
                     "            Electric heat load: {ELEC} W\n"
                     "                  RF heat load: {RF} W\n"
                     "                 Calculated Q0: {{Q0Val}}\n")

        report = reportStr.format(cavName=self.dataSession.container.name,
                                  runNum=self.num,
                                  TOT=round(self.totalHeatLoad, 2),
                                  ELEC=round(self.elecHeatLoad, 2),
                                  RF=round(self.rfHeatLoad, 2))

        if self.elecHeatLoadDes != 0:
            print(report.format(Q0Val=None))

        else:
            Q0 = '{:.2e}'.format(Decimal(self.q0))
            print(report.format(Q0Val=Q0))

    @staticmethod
    def calcQ0(grad, rfHeatLoad, avgPressure=None):
        # The initial Q0 calculation doesn't account for the temperature
        # variation of the 2 K helium
        uncorrectedQ0 = ((grad * 1000000) ** 2) / (939.3 * rfHeatLoad)

        # We can correct Q0 for the helium temperature!
        if avgPressure:
            tempFromPress = (avgPressure * 0.0125) + 1.705

            C1 = 271
            C2 = 0.0000726
            C3 = 0.00000214
            C4 = grad - 0.7
            C5 = 0.000000043
            C6 = -17.02
            C7 = C2 - (C3 * C4) + (C5 * (C4 ** 2))

            correctedQ0 = C1 / ((C7 / 2) * exp(C6 / 2)
                                + C1 / uncorrectedQ0
                                - (C7 / tempFromPress)
                                * exp(C6 / tempFromPress))
            return correctedQ0

        else:
            return uncorrectedQ0


def main():
    cryomodule = Cryomodule(cryModNumSLAC=12, cryModNumJLAB=2)
    for idx, cav in cryomodule.cavities.items():  # type: (int, Cavity)
        print(cav.gradPV)


if __name__ == '__main__':
    main()
